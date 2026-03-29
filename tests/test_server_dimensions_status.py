import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


@pytest.mark.asyncio
async def test_get_sku_returns_product_with_no_data_status(monkeypatch):
    async def fake_fetch(_sku):
        return {"sku": "1673537055", "name": "demo"}, "ok"

    async def fake_seller(_method_name, _sku):
        return {
            "sku": "1673537055",
            "status": "no_data",
            "dimensions": None,
            "categories": [{"id": "1", "level": "1", "name": "demo"}],
            "error": None,
        }

    monkeypatch.setattr(server, "_fetch_with_profile_recovery", fake_fetch)
    monkeypatch.setattr(server, "_call_seller_with_retry", fake_seller)

    result = await server.get_sku("1673537055")

    assert result == {
        "sku": "1673537055",
        "name": "demo",
        "dimensions": None,
        "seller_categories": [{"id": "1", "level": "1", "name": "demo"}],
        "seller_dimensions_status": "no_data",
        "seller_dimensions_detail": {
            "code": "seller_dimensions_not_found",
            "message": "Seller queried successfully but no dimensions found for SKU 1673537055",
            "sku": "1673537055",
            "seller_status": "no_data",
            "seller_error": None,
        },
    }


@pytest.mark.asyncio
async def test_variant_model_returns_per_sku_status(monkeypatch):
    async def fake_seller(_method_name, sku):
        if sku == "ok-sku":
            return {
                "sku": sku,
                "status": "ok",
                "dimensions": {"weight": 100.0, "depth": 200.0, "width": 150.0, "height": 20.0},
                "categories": [{"id": "1", "level": "1", "name": "cat-a"}],
                "error": None,
            }
        if sku == "no-data-sku":
            return {
                "sku": sku,
                "status": "no_data",
                "dimensions": None,
                "categories": [{"id": "2", "level": "2", "name": "cat-b"}],
                "error": None,
            }
        return {
            "sku": sku,
            "status": "request_failed",
            "dimensions": None,
            "categories": None,
            "error": "http_500",
        }

    monkeypatch.setattr(server, "_call_seller_with_retry", fake_seller)

    result = await server.variant_model(server.SkuListRequest(
        skus=["ok-sku", "no-data-sku", "failed-sku"]
    ))

    assert result == {
        "dimensions": {
            "ok-sku": {
                "weight": 100.0,
                "depth": 200.0,
                "width": 150.0,
                "height": 20.0,
            }
        },
        "results": {
            "ok-sku": {
                "sku": "ok-sku",
                "status": "ok",
                "dimensions": {
                    "weight": 100.0,
                    "depth": 200.0,
                    "width": 150.0,
                    "height": 20.0,
                },
                "categories": [{"id": "1", "level": "1", "name": "cat-a"}],
                "error": None,
            },
            "no-data-sku": {
                "sku": "no-data-sku",
                "status": "no_data",
                "dimensions": None,
                "categories": [{"id": "2", "level": "2", "name": "cat-b"}],
                "error": None,
            },
            "failed-sku": {
                "sku": "failed-sku",
                "status": "request_failed",
                "dimensions": None,
                "categories": None,
                "error": "http_500",
            },
        },
        "total": 1,
    }
