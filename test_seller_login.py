"""Quick test for seller login flow and API calls."""
import asyncio
import logging
from config import SELLER_EMAIL, SELLER_EMAIL_APP_PASSWORD, SELLER_CLIENT_ID, SELLER_STORAGE_STATE
from seller_login import get_seller_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

TEST_SKUS = ["2036172405", "1954292284", "1960628404"]

async def main():
    print(f"Logging in as {SELLER_EMAIL}, client_id={SELLER_CLIENT_ID}")
    session = await get_seller_session(
        SELLER_EMAIL, SELLER_EMAIL_APP_PASSWORD, SELLER_CLIENT_ID, SELLER_STORAGE_STATE
    )
    if not session:
        print("✗ Login failed")
        return

    print("✓ Seller session ready, URL:", session.page.url)

    # Test search-variant-model
    print("\nTesting fetch_variant_model...")
    for sku in TEST_SKUS:
        result = await session.fetch_variant_model(sku)
        print(f"  SKU {sku}: {result}")

    # Test data_v3
    print("\nTesting fetch_data_v3...")
    result = await session.fetch_data_v3(TEST_SKUS)
    if result:
        print(f"  data/v3 returned {len(result)} items")
        if result:
            print(f"  first item keys: {list(result[0].keys()) if result else 'none'}")
    else:
        print("  data/v3 returned no data")

    await session.close()

asyncio.run(main())
