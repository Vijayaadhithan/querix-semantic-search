def active_filter_names(filters: dict) -> list[str]:
    names = []
    for key, value in filters.items():
        if isinstance(value, dict):
            names.extend(
                f"{key}.{child_key}"
                for child_key, child_value in value.items()
                if child_value not in (None, "", [], {})
            )
        elif value not in (None, "", [], {}):
            names.append(key)
    return names
