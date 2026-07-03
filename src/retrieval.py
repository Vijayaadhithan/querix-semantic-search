import chromadb

from ollama_client import embed_text
from mysql_store import fetch_product_types_by_ids
from query_planner import OFFER_AD_TYPE, WANTED_AD_TYPE
from settings import (
    BM25_WEIGHT,
    CHROMA_DIR,
    COLLECTION_NAME,
    UNPRICED_RENTAL_FEE_CEILING,
    MYSQL_SEARCH_ID_COLUMN,
    MYSQL_TABLE,
    RRF_CONSTANT,
    SOFT_CATEGORY_BOOST,
    VECTOR_POST_FILTER_MAX_CANDIDATES,
    VECTOR_POST_FILTER_OVERFETCH_FACTOR,
    VECTOR_WEIGHT,
)


def load_collection(
    chroma_dir=CHROMA_DIR,
    collection_name=COLLECTION_NAME,
):
    client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        return client.get_collection(collection_name)
    except Exception as exc:
        raise RuntimeError(
            f"No vector collection {collection_name!r} found. "
            "Run the tenant ingestion job first."
        ) from exc


def metadata_matches_filters(
    metadata,
    source_name,
    resolved_filters,
    company_id=None,
) -> bool:
    if metadata.get("source_file") != source_name:
        return False
    if company_id is not None and metadata.get("company_id") != company_id:
        return False
    for key, expected in resolved_filters["categorical"].items():
        actual = metadata.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False

    minimum = resolved_filters.get("min_rental_fee")
    maximum = resolved_filters.get("max_rental_fee")
    if minimum is None and maximum is None:
        return True
    try:
        rental_fee = float(metadata.get("rental_fee"))
    except (TypeError, ValueError):
        return False
    if rental_fee <= UNPRICED_RENTAL_FEE_CEILING:
        return False
    if minimum is not None and rental_fee < minimum:
        return False
    if maximum is not None and rental_fee > maximum:
        return False
    return True


def vector_where_filter(
    source_name,
    resolved_filters,
    company_id=None,
):
    # Each tenant already owns an isolated vector collection. Adding
    # source_file/company_id to every Chroma query makes HNSW search fall back
    # to an expensive metadata-filtered path across the whole collection.
    # Source and tenant are still verified by metadata_matches_filters below.
    clauses = []
    for key, expected in resolved_filters.get("categorical", {}).items():
        if isinstance(expected, (list, tuple, set)):
            values = list(dict.fromkeys(expected))
            if not values:
                continue
            clauses.append({key: {"$in": values}})
        else:
            clauses.append({key: expected})
    minimum = resolved_filters.get("min_rental_fee")
    maximum = resolved_filters.get("max_rental_fee")
    if minimum is not None:
        clauses.append({"rental_fee": {"$gte": minimum}})
    if maximum is not None:
        clauses.append({"rental_fee": {"$lte": maximum}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def vector_search(
    query,
    collection,
    top_k=15,
    candidate_k=100,
    source_name=None,
    resolved_filters=None,
    embedding_provider=None,
    company_id=None,
):
    if collection.count() <= 0:
        return []

    query_embedding = (
        embedding_provider.embed_text(query)
        if embedding_provider is not None
        else embed_text(query)
    )
    query_options = {
        "query_embeddings": [query_embedding],
        "n_results": min(max(candidate_k, top_k), collection.count()),
        "include": ["documents", "metadatas", "distances"],
    }
    if source_name is not None and resolved_filters is not None:
        where_filter = vector_where_filter(
            source_name,
            resolved_filters,
            company_id,
        )
        if where_filter is not None:
            # Chroma's metadata-filtered query path can take many seconds on
            # large collections. Tenant collections are already isolated, so
            # retrieve a bounded HNSW window and enforce the exact same
            # constraints with metadata_matches_filters below.
            query_options["n_results"] = min(
                max(candidate_k, top_k)
                * VECTOR_POST_FILTER_OVERFETCH_FACTOR,
                VECTOR_POST_FILTER_MAX_CANDIDATES,
                collection.count(),
            )
    try:
        results = collection.query(**query_options)
    except TypeError as exc:
        # The pgvector compatibility collection in older deployments did not
        # yet expose Chroma's `where` keyword. Keep the correctness-preserving
        # metadata check below as a fallback.
        if "where" not in str(exc):
            raise
        query_options.pop("where", None)
        results = collection.query(**query_options)

    output = []
    for doc_id, text, metadata, distance in zip(
        results["ids"][0],
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        if (
            source_name is not None
            and resolved_filters is not None
            and not metadata_matches_filters(
                metadata,
                source_name,
                resolved_filters,
                company_id,
            )
        ):
            continue
        output.append(
            {
                "id": doc_id,
                "text": text,
                "metadata": metadata,
                "score": float(distance),
                "source": "vector",
            }
        )
        if len(output) >= top_k:
            break
    return output


def bm25_search(query, index, collection, resolved_filters, top_k=15):
    ranked = index.search(query, resolved_filters, top_k)
    if not ranked:
        return []

    scores = {item["doc_id"]: item["score"] for item in ranked}
    ordered_ids = [item["doc_id"] for item in ranked]
    data = collection.get(ids=ordered_ids, include=["documents", "metadatas"])
    documents = {
        doc_id: {"text": text, "metadata": metadata}
        for doc_id, text, metadata in zip(
            data["ids"],
            data["documents"],
            data["metadatas"],
        )
    }
    return [
        {
            "id": doc_id,
            "text": documents[doc_id]["text"],
            "metadata": documents[doc_id]["metadata"],
            "score": scores[doc_id],
            "source": "bm25",
        }
        for doc_id in ordered_ids
        if doc_id in documents
    ]


def related_tail_product_ids(
    index,
    resolved_filters,
    inferred_categories,
    target_ad_type,
    limit,
    exclude_doc_ids=None,
    exclude_product_ids=None,
    type_fetcher=fetch_product_types_by_ids,
    sort_order=None,
    allowed_ad_types: set[str] | None = None,
):
    """Return a stable filtered tail without requiring keyword relevance."""
    if limit <= 0:
        return []

    categorical = resolved_filters.get("categorical", {})
    category_filters = {}
    category_keys = {
        "main_category": "main_category_name",
        "subcategory": "subcategory_name",
    }
    has_explicit_category = any(
        metadata_key in categorical
        for metadata_key in category_keys.values()
    )
    if not has_explicit_category:
        for category_key, metadata_key in category_keys.items():
            value = (inferred_categories or {}).get(category_key)
            if value:
                category_filters[metadata_key] = value

    has_price_filter = any(
        key in resolved_filters
        for key in ("min_rental_fee", "max_rental_fee")
    )
    if not categorical and not category_filters and not has_price_filter:
        return []

    expected_types = (
        {str(value) for value in allowed_ad_types}
        if allowed_ad_types is not None
        else {
            WANTED_AD_TYPE
            if target_ad_type == "wanted"
            else OFFER_AD_TYPE
        }
    )
    excluded_products = {
        str(product_id)
        for product_id in (exclude_product_ids or set())
    }
    seen_products = set(excluded_products)
    product_ids = []
    offset = 0
    page_size = max(100, min(limit * 3, 500))

    while len(product_ids) < limit:
        rows = index.browse(
            resolved_filters,
            page_size,
            category_filters=category_filters,
            exclude_doc_ids=set(exclude_doc_ids or set()),
            offset=offset,
            sort_order=sort_order,
        )
        if not rows:
            break
        offset += len(rows)
        row_product_ids = [row["product_id"] for row in rows]
        product_types = type_fetcher(row_product_ids)
        for product_id in row_product_ids:
            identity = str(product_id)
            if identity in seen_products:
                continue
            seen_products.add(identity)
            if product_types.get(identity) not in expected_types:
                continue
            product_ids.append(product_id)
            if len(product_ids) >= limit:
                break
        if len(rows) < page_size:
            break

    return product_ids


def merge_results(
    vector_results,
    bm25_results,
    inferred_categories=None,
    rrf_constant=RRF_CONSTANT,
    vector_weight=VECTOR_WEIGHT,
    bm25_weight=BM25_WEIGHT,
    soft_category_boost=SOFT_CATEGORY_BOOST,
):
    merged = {}
    ranked_sources = (
        ("vector", vector_results, vector_weight),
        ("bm25", bm25_results, bm25_weight),
    )
    for source, results, weight in ranked_sources:
        for rank, item in enumerate(results, start=1):
            if item["id"] not in merged:
                merged[item["id"]] = {
                    **item,
                    "source": source,
                    "fusion_score": 0.0,
                }
            elif source not in merged[item["id"]]["source"].split("+"):
                merged[item["id"]]["source"] += f"+{source}"
            merged[item["id"]]["fusion_score"] += weight / (
                rrf_constant + rank
            )

    inferred_categories = inferred_categories or {}
    metadata_keys = {
        "main_category": "main_category_name",
        "subcategory": "subcategory_name",
    }
    for item in merged.values():
        metadata = item.get("metadata") or {}
        for category_key, metadata_key in metadata_keys.items():
            expected = inferred_categories.get(category_key)
            if expected is not None and metadata.get(metadata_key) == expected:
                item["fusion_score"] += soft_category_boost

    return sorted(
        merged.values(),
        key=lambda item: item["fusion_score"],
        reverse=True,
    )


def extract_product_ids(
    candidates,
    search_table=MYSQL_TABLE,
    search_id_column=MYSQL_SEARCH_ID_COLUMN,
):
    product_ids = []
    seen = set()

    for result in candidates:
        metadata = result.get("metadata") or {}
        if metadata.get("source_type") not in {"mysql", "postgres"}:
            continue
        if metadata.get("source_table") != search_table:
            continue

        product_id = metadata.get(search_id_column)
        if (
            product_id is None
            and metadata.get("primary_key_column") == search_id_column
        ):
            product_id = metadata.get("primary_key_value")
        if product_id is None:
            continue

        identity = str(product_id)
        if identity in seen:
            continue
        seen.add(identity)
        product_ids.append(product_id)
    return product_ids


def filter_candidates_by_ad_type(
    candidates,
    target_ad_type: str,
    connection=None,
    type_fetcher=None,
    search_table=MYSQL_TABLE,
    search_id_column=MYSQL_SEARCH_ID_COLUMN,
    allowed_ad_types: set[str] | None = None,
):
    expected_types = (
        {str(value) for value in allowed_ad_types}
        if allowed_ad_types is not None
        else {
            WANTED_AD_TYPE
            if target_ad_type == "wanted"
            else OFFER_AD_TYPE
        }
    )
    product_ids = extract_product_ids(
        candidates,
        search_table=search_table,
        search_id_column=search_id_column,
    )
    if type_fetcher is None:
        product_types = fetch_product_types_by_ids(
            product_ids,
            connection=connection,
        )
    else:
        product_types = type_fetcher(product_ids)

    filtered = []
    for candidate in candidates:
        candidate_ids = extract_product_ids(
            [candidate],
            search_table=search_table,
            search_id_column=search_id_column,
        )
        if not candidate_ids:
            continue
        if product_types.get(str(candidate_ids[0])) in expected_types:
            filtered.append(candidate)
    return filtered
