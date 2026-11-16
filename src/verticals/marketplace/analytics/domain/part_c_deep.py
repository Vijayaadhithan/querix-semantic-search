import logging

from .listing_metrics import listing_completeness_metrics
from .pricing_metrics import city_pricing_metrics
from .scope import MarketplaceScope
from .supply_metrics import inventory_metrics, supply_distribution_metrics
from .user_metrics import user_population_metrics

LOGGER = logging.getLogger(__name__)


def process_part_c(data, *, scope: MarketplaceScope):
    LOGGER.info("Processing Part C: Cross-CSV Deep Analytics")
    results = {}

    all_ads = data["ads"]
    ads = scope.active_ads(all_ads)
    users = data["users"]
    cats = data["categories"]
    subcats = data["sub_categories"]
    states = data["states"]
    location = data["location"]
    attrs = data["attributes"]
    attr_vals = data["attribute_values"]
    ads_attrs = data["ads_attributes"]

    total_ads = len(ads)
    total_users = len(users)

    ads_by_city, cat_names = supply_distribution_metrics(
        results, ads, cats, location, subcats
    )
    user_population_metrics(results, ads, states, total_users, users)
    city_state = inventory_metrics(
        results, ads, ads_by_city, all_ads, cats, location, subcats, total_ads
    )
    city_pricing_metrics(results, ads, ads_by_city, cat_names, city_state, location)
    listing_completeness_metrics(results, ads, ads_attrs, attr_vals, attrs, total_ads)

    return results
