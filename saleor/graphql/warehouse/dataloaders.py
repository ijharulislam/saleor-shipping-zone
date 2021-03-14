from collections import defaultdict
from typing import DefaultDict, Iterable, List, Optional, Tuple

from django.conf import settings

from ...warehouse.models import Stock
from ..core.dataloaders import DataLoader

ShippingZone = Optional[str]
VariantIdAndShippingZone = Tuple[int, ShippingZone]


class AvailableQuantityByProductVariantIdAndShippingZoneCodeLoader(
    DataLoader[VariantIdAndShippingZone, int]
):
    """Calculates available variant quantity based on variant ID and country code.

    For each country code, for each shipping zone supporting that country,
    calculate the maximum available quantity, then return either that number
    or the maximum allowed checkout quantity, whichever is lower.
    """

    context_key = "stock_by_productvariant_and_country"

    def batch_load(self, keys):
        # Split the list of keys by country first. A typical query will only touch
        # a handful of unique countries but may access thousands of product variants
        # so it's cheaper to execute one query per country.
        variants_by_shipping_zone: DefaultDict[ShippingZone, List[int]] = defaultdict(list)
        for variant_id, shipping_zone in keys:
            variants_by_shipping_zone[shipping_zone].append(variant_id)

        # For each country code execute a single query for all product variants.
        quantity_by_variant_and_shipping_zone: DefaultDict[
            VariantIdAndShippingZone, int
        ] = defaultdict(int)
        for shipping_zone, variant_ids in variants_by_shipping_zone.items():
            quantities = self.batch_load_shipping_zone(shipping_zone, variant_ids)
            for variant_id, quantity in quantities:
                quantity_by_variant_and_shipping_zone[(
                    variant_id, shipping_zone)] = quantity

        return [quantity_by_variant_and_shipping_zone[key] for key in keys]

    def batch_load_shipping_zone(
        self, shipping_zone: ShippingZone, variant_ids: Iterable[int]
    ) -> Iterable[Tuple[int, int]]:
        results = Stock.objects.filter(product_variant_id__in=variant_ids)
        if shipping_zone:
            results.filter(warehouse__shipping_zones__name=shipping_zone)
        results = results.annotate_available_quantity()
        results = results.values_list(
            "product_variant_id", "warehouse__shipping_zones", "available_quantity"
        )

        # A single country code (or a missing country code) can return results from
        # multiple shipping zones. We want to combine all quantities within a single
        # zone and then find out which zone contains the highest total.
        quantity_by_shipping_zone_by_product_variant: DefaultDict[
            int, DefaultDict[int, int]
        ] = defaultdict(lambda: defaultdict(int))
        for variant_id, shipping_zone_id, quantity in results:
            quantity_by_shipping_zone_by_product_variant[variant_id][
                shipping_zone_id
            ] += quantity
        quantity_map: DefaultDict[int, int] = defaultdict(int)
        for (
            variant_id,
            quantity_by_shipping_zone,
        ) in quantity_by_shipping_zone_by_product_variant.items():
            quantity_map[variant_id] = max(quantity_by_shipping_zone.values())

        # Return the quantities after capping them at the maximum quantity allowed in
        # checkout. This prevent users from tracking the store's precise stock levels.
        return [
            (
                variant_id,
                min(quantity_map[variant_id], settings.MAX_CHECKOUT_LINE_QUANTITY),
            )
            for variant_id in variant_ids
        ]
