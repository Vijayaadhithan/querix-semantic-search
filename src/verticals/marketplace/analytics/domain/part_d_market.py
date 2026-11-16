import logging

from .market_growth_metrics import market_growth_metrics, seller_activity_metrics
from .market_supply_metrics import market_supply_metrics
from .scope import MarketplaceScope

LOGGER = logging.getLogger(__name__)


def process_part_d(data, *, scope: MarketplaceScope):
    LOGGER.info("Processing Part D: Customer-Facing Market Intelligence")
    results = {}

    all_ads = data["ads"]
    ads = scope.active_ads(all_ads)
    cats = data["categories"]
    subcats = data["sub_categories"]
    location = data["location"]
    states = data["states"]
    users = data["users"]

    cat_names = cats.set_index("id")["name"].to_dict()
    subcat_to_cat = subcats.set_index("id")["categoryId"].to_dict()
    city_names = location.set_index("id")["city"].to_dict()
    state_names = states.set_index("id")["name"].to_dict()

    market_supply_metrics(
        results,
        ads,
        cat_names,
        city_names,
        location,
        state_names,
        subcat_to_cat,
        subcats,
    )
    market_growth_metrics(results, ads, all_ads, state_names, users)
    seller_activity_metrics(results, ads, all_ads, users)

    return results
