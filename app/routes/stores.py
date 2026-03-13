"""
Store and postal code discovery endpoints
"""

from fastapi import APIRouter, HTTPException, status, Query
from typing import List, Optional
from app.providers.flipp.api import Api as FlippApi
import logging

from app.models.schemas import (
    PostalCodeDiscoveryRequest,
    PostalCodeDiscoveryResponse,
    StoreInfo,
    DealInfo,
    ErrorResponse
)
from app.services.store_service import StoreService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/postal-code", tags=["stores"])


@router.post("/discover",
            #response_model=PostalCodeDiscoveryResponse,
             responses={
                 404: {"model": ErrorResponse, "description": "No stores found"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def discover_postal_code(request: PostalCodeDiscoveryRequest):
    """
    Discover stores and deals for a postal code.

    Fetches all available stores and current deals for the specified postal code.
    This is the first step before generating meal plans.

    - **postal_code**: Canadian postal code (e.g., "M5V3A8")

    Returns store count, deal count, and list of stores found.
    """
    my_flip = FlippApi(postal_code=request.postal_code, local e="en")
    all_flyers = my_flip.get_flyers()
    grocery_flyers = [f for f in all_flyers["flyers"] if "Groceries" in f["categories"]]

    grocery_stores: List[StoreInfo] = []
    
    for flyer in grocery_flyers:
        grocery_stores.append(StoreInfo(
            store_id=flyer["merchant_id"],
            name=flyer["merchant"],
            chain=flyer["merchant"],
            postal_code=flyer["postal_code"],
            address="Unknown",
            city="Unknown",
            province="Unknown",
        ))

    rc = PostalCodeDiscoveryResponse(
        postal_code=request.postal_code,
        stores_found=len(grocery_stores),
        deals_count=23,
        stores=grocery_stores,
        job_id="19",
        message="test message",
    )


    return rc
    try:
        postal_code = request.postal_code

        # Get stores
        stores = StoreService.get_stores_by_postal_code(postal_code)

        if not stores:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No stores found for postal code {postal_code}"
            )

        # Get deals
        deals = StoreService.get_current_deals_by_postal_code(postal_code)

        # Convert stores to response format
        store_infos = [
            StoreInfo(
                store_id=store["store_id"],
                name=store["name"],
                chain=store["chain"],
                postal_code=store["postal_code"],
                address=store["address"],
                city=store["city"],
                province=store["province"]
            )
            for store in stores
        ]

        logger.info(f"Found {len(stores)} stores and {len(deals)} deals for {postal_code}")

        return PostalCodeDiscoveryResponse(
            postal_code=postal_code,
            stores_found=len(stores),
            deals_count=len(deals),
            stores=store_infos,
            job_id=None,  # Would be populated if using background jobs
            message=f"Found {len(stores)} stores with {len(deals)} active deals"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error discovering postal code {request.postal_code}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to discover postal code"
        )


@router.get("/deals/{postal_code}",
            response_model=List[DealInfo],
            responses={
                404: {"model": ErrorResponse, "description": "No deals found"}
            })
async def get_deals(
    postal_code: str,
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: Optional[int] = Query(50, ge=1, le=200, description="Maximum number of deals to return")
) -> List[DealInfo]:
    """
    Get current deals for a postal code.

    Fetch active deals with optional category filtering.

    - **postal_code**: Postal code to search
    - **category**: Optional category filter (e.g., "Produce", "Meat & Poultry")
    - **limit**: Maximum number of results (default: 50, max: 200)
    """
    try:
        deals = StoreService.get_current_deals_by_postal_code(postal_code, category)

        if not deals:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No deals found for postal code {postal_code}"
            )

        # Limit results
        deals = deals[:limit]

        deal_infos = [
            DealInfo(
                deal_id=deal["deal_id"],
                product_name=deal["product_name"],
                brand=deal["brand"],
                sale_price=deal["sale_price"],
                regular_price=deal["regular_price"],
                discount_percentage=deal["discount_percentage"],
                unit=deal["unit"],
                category=deal["category"],
                valid_from=deal["valid_from"],
                valid_until=deal["valid_until"],
                store_name=deal["store_name"],
                chain=deal["chain"]
            )
            for deal in deals
        ]

        return deal_infos

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching deals for {postal_code}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch deals"
        )


@router.get("/top-deals/{postal_code}",
            response_model=List[DealInfo])
async def get_top_deals(
    postal_code: str,
    limit: int = Query(20, ge=1, le=50, description="Number of top deals")
) -> List[DealInfo]:
    """
    Get top deals by discount percentage.

    Returns the best deals sorted by discount percentage.
    """
    deals = StoreService.get_top_deals(postal_code, limit)

    deal_infos = [
        DealInfo(
            deal_id=deal.get("deal_id", 0),
            product_name=deal["product_name"],
            brand=deal["brand"],
            sale_price=deal["sale_price"],
            regular_price=deal["regular_price"],
            discount_percentage=deal["discount_percentage"],
            unit=deal["unit"],
            category=deal["category"],
            valid_from=deal.get("valid_from"),
            valid_until=deal.get("valid_until"),
            store_name=deal["store_name"],
            chain=deal["chain"]
        )
        for deal in deals
    ]

    return deal_infos


@router.get("/search/{postal_code}",
            response_model=List[DealInfo])
async def search_deals(
    postal_code: str,
    q: str = Query(..., min_length=2, description="Search term (product name or brand)")
) -> List[DealInfo]:
    """
    Search deals by product name or brand.

    - **postal_code**: Postal code to search in
    - **q**: Search query (minimum 2 characters)
    """
    deals = StoreService.search_deals(postal_code, q)

    if not deals:
        return []

    deal_infos = [
        DealInfo(
            deal_id=deal["deal_id"],
            product_name=deal["product_name"],
            brand=deal["brand"],
            sale_price=deal["sale_price"],
            regular_price=deal["regular_price"],
            discount_percentage=deal["discount_percentage"],
            unit=deal["unit"],
            category=deal["category"],
            valid_from=deal.get("valid_from"),
            valid_until=deal.get("valid_until"),
            store_name=deal["store_name"],
            chain=deal["chain"]
        )
        for deal in deals
    ]

    return deal_infos
