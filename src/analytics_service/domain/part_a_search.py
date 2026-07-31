import logging
import re
from collections import Counter

import pandas as pd

from .config import TYPO_INDICATORS
from .search.insights import build_additional_search_insights
from .utils import (
    classify_search_query,
    detect_language,
    detect_rental_duration,
    extract_brands,
    extract_locations,
    has_typo,
    is_b2b_query,
    is_gibberish,
    is_route_query,
)

LOGGER = logging.getLogger(__name__)


def _percentage(count, total):
    return round(count / total * 100, 1) if total else 0


def process_part_a(data):
    LOGGER.info("Processing Part A: Search Demand Intelligence")
    sh = data['search_history'].copy()
    api = data['api_usage'].copy()
    results = {}

    queries = sh['query_text'].tolist()
    total_q = len(queries)

    # Q1: Top searched categories
    cat_counts = Counter()
    query_cats = []
    for q in queries:
        cats = classify_search_query(q)
        query_cats.append(cats)
        for c in cats:
            cat_counts[c] += 1
    top_cats = cat_counts.most_common(20)
    results['q1_category_distribution'] = {
        'labels': [c[0] for c in top_cats],
        'values': [c[1] for c in top_cats],
        'title': 'Top Searched Categories',
        'chart_type': 'bar'
    }

    # Q2: Products vs Services vs Accommodation %
    type_counts = {'Products': 0, 'Services': 0, 'Accommodation': 0, 'Other': 0}
    for cats in query_cats:
        is_service = 'Services & Professionals' in cats
        is_accom = 'Home & Accommodation' in cats
        is_product = any(c not in ['Services & Professionals', 'Home & Accommodation', 'Other / Uncategorized'] for c in cats)
        if is_service:
            type_counts['Services'] += 1
        elif is_accom:
            type_counts['Accommodation'] += 1
        elif is_product:
            type_counts['Products'] += 1
        else:
            type_counts['Other'] += 1
    results['q2_product_vs_service'] = {
        'labels': list(type_counts.keys()),
        'values': list(type_counts.values()),
        'title': 'Products vs Services vs Accommodation',
        'chart_type': 'doughnut'
    }

    # Q3: Top trending search terms (word frequency)
    word_freq = Counter()
    stop_words = {'for', 'in', 'on', 'of', 'to', 'and', 'the', 'a', 'an', 'or', 'is',
                  'at', 'by', 'from', 'with', 'per', 'if', 'any', 'all', 'i', 'me',
                  'my', 'we', 'our', 'you', 'your', 'it', 'its', 'am', 'be', 'no',
                  'not', 'do', 'does', 'did', 'has', 'have', 'had', 'will', 'can',
                  'could', 'should', 'would', 'may', 'shall', 'here', 'there', 'one',
                  'this', 'that', 'these', 'those', '-', '&'}
    for q in queries:
        words = re.findall(r'[a-zA-Z]+', q.lower())
        for w in words:
            if w not in stop_words and len(w) > 2:
                word_freq[w] += 1
    top_terms = word_freq.most_common(30)
    results['q3_trending_terms'] = {
        'labels': [t[0] for t in top_terms],
        'values': [t[1] for t in top_terms],
        'title': 'Top 30 Trending Search Terms',
        'chart_type': 'bar'
    }

    # Q4: Brand/model mentions
    brand_counts = Counter()
    for q in queries:
        brands = extract_brands(q)
        for b in brands:
            brand_counts[b] += 1
    top_brands = brand_counts.most_common(20)
    results['q4_brand_mentions'] = {
        'labels': [b[0] for b in top_brands],
        'values': [b[1] for b in top_brands],
        'title': 'Most Searched Brands/Models',
        'chart_type': 'bar'
    }

    # Q5: Location mentions in queries
    loc_counts = Counter()
    for q in queries:
        locs = extract_locations(q)
        for location in locs:
            loc_counts[location] += 1
    top_locs = loc_counts.most_common(20)
    results['q5_location_mentions'] = {
        'labels': [location[0] for location in top_locs],
        'values': [location[1] for location in top_locs],
        'title': 'Most Mentioned Locations in Searches',
        'chart_type': 'bar'
    }

    # Q6: Rental duration preferences
    dur_counts = Counter()
    for q in queries:
        dur = detect_rental_duration(q)
        dur_counts[dur] += 1
    results['q6_rental_duration'] = {
        'labels': list(dur_counts.keys()),
        'values': list(dur_counts.values()),
        'title': 'Rental Duration Preferences',
        'chart_type': 'doughnut'
    }

    # Q7: Zero-result queries
    merged = sh.merge(api[['request_id', 'total_results', 'result_count']], on='request_id', how='left')
    zero_results = merged[merged['total_results'] == 0]
    zero_queries = zero_results['query_text'].dropna().tolist()
    results['q7_zero_results'] = {
        'queries': zero_queries[:50],
        'total_zero': len(zero_queries),
        'total_searches': total_q,
        'percentage': _percentage(len(zero_queries), total_q),
        'title': 'Searches with Zero Results (Unmet Demand)',
        'chart_type': 'stat'
    }

    # Q8: Route/trip searches
    route_queries = [q for q in queries if is_route_query(q)]
    results['q8_route_searches'] = {
        'queries': route_queries,
        'count': len(route_queries),
        'percentage': _percentage(len(route_queries), total_q),
        'title': 'Route/Trip-Based Searches',
        'chart_type': 'list'
    }

    # Q9: Top searched professions
    profession_kws = {
        'Acting Driver': ['acting driver', 'driver'],
        'Massage Therapist': ['massage', 'therapist', 'massager'],
        'Cook / Parotta Master': ['cook', 'parotta', 'chef', 'caterer', 'biriyani'],
        'Electrician': ['electrician', 'electrical'],
        'Photographer': ['photographer', 'photography'],
        'Trainer / Instructor': ['trainer', 'instructor', 'training'],
        'Tutor / Teacher': ['tutor', 'tuition', 'teacher'],
        'Nurse / Medical': ['nurse', 'doctor', 'medical', 'psychiatrist'],
        'Carpenter / Painter': ['carpenter', 'painter', 'mason'],
        'Plumber': ['plumber'],
        'Helper / Companion': ['helper', 'companion'],
        'Writer / Editor': ['writer', 'editor', 'data entry', 'typing'],
        'Cleaner / Climber': ['cleaner', 'climber'],
        'Delivery / Logistics': ['delivery', 'loading', 'unloading'],
    }
    prof_counts = Counter()
    for q in queries:
        ql = q.lower()
        for prof, kws in profession_kws.items():
            if any(kw in ql for kw in kws):
                prof_counts[prof] += 1
                break
    top_profs = prof_counts.most_common(15)
    results['q9_professions'] = {
        'labels': [p[0] for p in top_profs],
        'values': [p[1] for p in top_profs],
        'title': 'Most Searched Professions/Services',
        'chart_type': 'bar'
    }

    # Q10: Language patterns
    lang_counts = Counter()
    for q in queries:
        lang = detect_language(q)
        lang_counts[lang] += 1
    results['q10_language'] = {
        'labels': list(lang_counts.keys()),
        'values': list(lang_counts.values()),
        'title': 'Search Language Distribution',
        'chart_type': 'doughnut'
    }

    # Q11: Typo/spelling errors
    typo_queries = [(q, True) for q in queries if has_typo(q)]
    results['q11_typos'] = {
        'count': len(typo_queries),
        'percentage': _percentage(len(typo_queries), total_q),
        'examples': [t[0] for t in typo_queries[:20]],
        'known_typos': [{'typo': t, 'correction': c} for t, c in TYPO_INDICATORS],
        'title': 'Searches with Spelling Errors',
        'chart_type': 'stat'
    }

    # Q12: Query length distribution
    query_lengths = [len(q.split()) for q in queries]
    char_lengths = [len(q) for q in queries]
    len_dist = Counter()
    for query_length in query_lengths:
        if query_length <= 1:
            len_dist['1 word'] += 1
        elif query_length <= 2:
            len_dist['2 words'] += 1
        elif query_length <= 3:
            len_dist['3 words'] += 1
        elif query_length <= 5:
            len_dist['4-5 words'] += 1
        elif query_length <= 8:
            len_dist['6-8 words'] += 1
        else:
            len_dist['9+ words'] += 1
    results['q12_query_length'] = {
        'labels': ['1 word', '2 words', '3 words', '4-5 words', '6-8 words', '9+ words'],
        'values': [len_dist.get(k, 0) for k in ['1 word', '2 words', '3 words', '4-5 words', '6-8 words', '9+ words']],
        'avg_words': (
            round(sum(query_lengths) / len(query_lengths), 1)
            if query_lengths else 0
        ),
        'avg_chars': (
            round(sum(char_lengths) / len(char_lengths), 1)
            if char_lengths else 0
        ),
        'max_words': max(query_lengths, default=0),
        'min_words': min(query_lengths, default=0),
        'title': 'Query Length Distribution',
        'chart_type': 'bar'
    }

    # Q13: Gibberish detection
    gibberish = [q for q in queries if is_gibberish(q)]
    results['q13_gibberish'] = {
        'count': len(gibberish),
        'percentage': _percentage(len(gibberish), total_q),
        'examples': gibberish[:15],
        'title': 'Gibberish/Accidental Queries',
        'chart_type': 'stat'
    }

    # Q14: Location specificity
    loc_specific = [q for q in queries if extract_locations(q)]
    results['q14_location_specificity'] = {
        'count': len(loc_specific),
        'percentage': _percentage(len(loc_specific), total_q),
        'title': 'Searches with Location Specificity',
        'chart_type': 'stat'
    }

    # Q15: Search volume over time
    sh['created_at'] = pd.to_datetime(sh['created_at'])
    sh['hour'] = sh['created_at'].dt.hour
    sh['minute'] = sh['created_at'].dt.minute
    hourly = sh.groupby('hour').size().reindex(range(24), fill_value=0)
    results['q15_search_volume'] = {
        'labels': [f"{h}:00" for h in range(24)],
        'values': hourly.tolist(),
        'title': 'Search Volume by Hour of Day',
        'chart_type': 'line'
    }

    # Q16: New subcategory suggestions
    uncat = [q for q, cats in zip(queries, query_cats) if 'Other / Uncategorized' in cats]
    results['q16_new_subcategories'] = {
        'queries': uncat[:30],
        'count': len(uncat),
        'title': 'Queries Suggesting New Categories',
        'chart_type': 'list'
    }

    # Q17: High-demand cities (from searches)
    results['q17_high_demand_cities'] = results['q5_location_mentions'].copy()
    results['q17_high_demand_cities']['title'] = 'High-Demand Cities from Search Queries'

    # Q18: Vehicle-specific searches
    vehicle_queries = []
    for q in queries:
        ql = q.lower()
        vehicle_kws = ['car', 'bike', 'truck', 'van', 'bus', 'auto', 'scooter', 'tata',
                       'maruti', 'mahindra', 'eicher', 'bajaj', 'honda', 'yamaha',
                       'royal enfield', 'hyundai', 'tvs', 'piaggio', 'vehicle', 'two wheeler',
                       'three wheeler', 'four wheeler', 'lcv']
        if any(kw in ql for kw in vehicle_kws):
            vehicle_queries.append(q)
    vehicle_types = Counter()
    for q in vehicle_queries:
        ql = q.lower()
        if any(w in ql for w in ['car', 'alto', 'swift', 'ertiga', 'innova', 'civic', 'xcent', 'baleno', 'eeco', 'omni', 'qualis']):
            vehicle_types['Cars'] += 1
        elif any(w in ql for w in ['bike', 'two wheeler', 'rx', 'r15', 'hunter', 'unicorn', 'hf deluxe', 'tnt', 'splendor']):
            vehicle_types['Bikes'] += 1
        elif any(w in ql for w in ['truck', 'lcv', 'container', 'open body', 'eicher']):
            vehicle_types['Trucks / LCV'] += 1
        elif any(w in ql for w in ['auto', 'ape', 'three wheeler', 'qute']):
            vehicle_types['Autos / 3-Wheeler'] += 1
        elif any(w in ql for w in ['van', 'bus', 'winger', 'seater']):
            vehicle_types['Vans / Buses'] += 1
        elif any(w in ql for w in ['scooter', 'dio', 'scooty', 'pep']):
            vehicle_types['Scooters'] += 1
        elif any(w in ql for w in ['tata ace', 'ace', 'dost', 'intra', 'bolero', 'pickup']):
            vehicle_types['Mini Trucks / Pickup'] += 1
        else:
            vehicle_types['Other Vehicle'] += 1
    results['q18_vehicle_searches'] = {
        'labels': list(vehicle_types.keys()),
        'values': list(vehicle_types.values()),
        'total': len(vehicle_queries),
        'percentage': _percentage(len(vehicle_queries), total_q),
        'sample_queries': vehicle_queries[:20],
        'title': 'Vehicle Rental Searches Breakdown',
        'chart_type': 'doughnut'
    }

    # Q19: Food industry searches
    food_queries = []
    for q in queries:
        ql = q.lower()
        food_kws = ['cook', 'chef', 'parotta', 'biriyani', 'caterer', 'food', 'kitchen',
                     'restaurant', 'hotel', 'bar', 'stove', 'cooking', 'veg']
        if any(kw in ql for kw in food_kws):
            food_queries.append(q)
    results['q19_food_searches'] = {
        'queries': food_queries,
        'count': len(food_queries),
        'percentage': _percentage(len(food_queries), total_q),
        'title': 'Food Industry Searches',
        'chart_type': 'list'
    }

    # Q20: B2B demand
    b2b_queries = [q for q in queries if is_b2b_query(q)]
    results['q20_b2b_demand'] = {
        'queries': b2b_queries,
        'count': len(b2b_queries),
        'percentage': _percentage(len(b2b_queries), total_q),
        'title': 'B2B Demand Indicators',
        'chart_type': 'list'
    }

    # New Additions for Advanced Search Analytics

    # Q85: Zero Results in High-Demand Cities
    zero_loc_counts = Counter()
    for q in zero_queries:
        locs = extract_locations(q)
        for location in locs:
            zero_loc_counts[location] += 1
    top_zero_locs = zero_loc_counts.most_common(15)
    results['q85_zero_results_cities'] = {
        'labels': [location[0] for location in top_zero_locs],
        'values': [location[1] for location in top_zero_locs],
        'title': 'Unmet Demand Locations (Zero Results)',
        'chart_type': 'bar'
    }

    # Q86: Most Common Unfulfilled Brands
    zero_brand_counts = Counter()
    for q in zero_queries:
        brands = extract_brands(q)
        for b in brands:
            zero_brand_counts[b] += 1
    top_zero_brands = zero_brand_counts.most_common(15)
    results['q86_unfulfilled_brands'] = {
        'labels': [b[0] for b in top_zero_brands],
        'values': [b[1] for b in top_zero_brands],
        'title': 'Most Common Unfulfilled Brands',
        'chart_type': 'bar'
    }

    # Q87: Zero results by Rental Duration
    zero_dur_counts = Counter()
    for q in zero_queries:
        dur = detect_rental_duration(q)
        zero_dur_counts[dur] += 1
    results['q87_unfulfilled_duration'] = {
        'labels': list(zero_dur_counts.keys()),
        'values': list(zero_dur_counts.values()),
        'title': 'Unmet Demand by Rental Duration',
        'chart_type': 'doughnut'
    }

    results.update(build_additional_search_insights(data))
    LOGGER.info("Part A complete: %d questions processed", len(results))
    return results
