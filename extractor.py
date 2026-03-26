"""
Extract product data from a stable Ozon product page.
"""
import re
import json
import logging
import time
import weakref

log = logging.getLogger(__name__)

_PAGE_OBSERVATIONS = weakref.WeakKeyDictionary()


def _page_observation(page):
    obs = _PAGE_OBSERVATIONS.get(page)
    if obs is None:
        obs = {
            "last_main_nav_at": 0.0,
            "last_main_response_status": None,
            "last_main_response_url": "",
        }
        _PAGE_OBSERVATIONS[page] = obs
    return obs


def attach_page_observers(page) -> None:
    obs = _page_observation(page)
    if obs.get("attached"):
        return

    obs["attached"] = True
    obs["last_main_nav_at"] = time.monotonic()

    def on_frame_navigated(frame):
        try:
            if frame == page.main_frame:
                obs["last_main_nav_at"] = time.monotonic()
        except Exception:
            pass

    def on_response(resp):
        try:
            req = resp.request
            if req.resource_type == "document" and req.frame == page.main_frame and req.is_navigation_request():
                obs["last_main_response_status"] = resp.status
                obs["last_main_response_url"] = resp.url
        except Exception:
            pass

    page.on("framenavigated", on_frame_navigated)
    page.on("response", on_response)


def _calculate_real_price(card_price: float, price: float) -> float:
    """Mirror of calculateRealPriceCore from extension."""
    if card_price > 0 and price > card_price:
        return round((price - card_price) * 2.2 + price)
    return round(price)


def _clean_price(s: str) -> float:
    """Parse Ozon price string like '1 219,50 ₽' or '69.02 ¥'."""
    s = re.sub(r'[^\d.,]', '', s.replace('\u2009', '').replace('\u00a0', ''))
    s = s.replace(',', '.')
    # Remove thousands-separator dots (e.g. 1.219.50 → only last dot is decimal)
    parts = s.split('.')
    if len(parts) > 2:
        s = ''.join(parts[:-1]) + '.' + parts[-1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def _normalize_ozon_image_url(url: str) -> str:
    if not url:
        return ""
    url = re.sub(r"^https://ir-\d+\.ozonstatic\.cn/", "https://ir.ozone.ru/", url)
    return url


def _to_wc_image_url(url: str, size: int) -> str:
    if not url:
        return ""
    clean_url = re.sub(r"/wc\d+/", "/", url)
    idx = clean_url.rfind("/")
    if idx == -1:
        return clean_url
    return f"{clean_url[:idx]}/wc{size}{clean_url[idx:]}"


def _merge_attributes(existing: list[dict], extra: list[dict]) -> list[dict]:
    seen = {
        (
            str(item.get("key") or ""),
            str(item.get("name") or ""),
            str(item.get("value") or ""),
        )
        for item in existing
    }
    merged = list(existing)
    for item in extra:
        sig = (
            str(item.get("key") or ""),
            str(item.get("name") or ""),
            str(item.get("value") or ""),
        )
        if sig not in seen:
            seen.add(sig)
            merged.append(item)
    return merged


def _find_attr_value(attributes: list[dict], *keys: str) -> str:
    for item in attributes:
        key = str(item.get("key") or "")
        name = str(item.get("name") or "")
        if key in keys or name in keys:
            return str(item.get("value") or "").strip()
    return ""


def _format_size_for_spec(size_value: str) -> str:
    if not size_value:
        return ""
    parts = [p.strip() for p in size_value.split(",") if p.strip()]
    if len(parts) >= 2:
        return f"{parts[0]}–{parts[-1]} RU"
    return parts[0]


def _build_specifications(attributes: list[dict]) -> str:
    color = _find_attr_value(attributes, "Color", "Цвет")
    ru_size = _format_size_for_spec(_find_attr_value(attributes, "RussianSizeClothes", "Российский размер"))
    maker_size = _find_attr_value(attributes, "SizeManufacturer", "Размер производителя")
    parts = [p for p in [color, ru_size, maker_size] if p]
    return " / ".join(parts)


def extract_variant_from_api(page1_widget_states: dict, page2_widget_states: dict, sku: str) -> dict:
    """
    Build a VariantFullData dict from captured Page1 + Page2 widgetStates.
    Mirrors the extension's parseFromWidgetStates + fetchCharacteristicsAndDescription logic.
    """
    result: dict = {
        "variant_id": sku,
        "sku": sku,
        "name": "",
        "specifications": "",
        "image_url": "",
        "images": [],
        "price": 0.0,
        "cardPrice": 0.0,
        "realPrice": 0.0,
        "original_price": None,
        "available": True,
        "description": "",
        "attributes": [],
        "typeNameRu": "",
        "link": "",
    }

    # ── PAGE 1 ──────────────────────────────────────────────
    keys1 = list(page1_widget_states.keys())

    # name
    heading_key = next((k for k in keys1 if 'webProductHeading' in k), None)
    if heading_key:
        try:
            result["name"] = json.loads(page1_widget_states[heading_key]).get('title', '')
        except Exception:
            pass

    # price
    price_key = next((k for k in keys1 if re.match(r'^webPrice-\d+-', k)), None)
    if price_key:
        try:
            pd = json.loads(page1_widget_states[price_key])
            card_price = _clean_price(pd.get('cardPrice', '') or '')
            price = _clean_price(pd.get('price', '') or '')
            original = _clean_price(pd.get('originalPrice', '') or '')
            result['cardPrice'] = card_price
            result['price'] = price
            result['realPrice'] = _calculate_real_price(card_price, price)
            if original > 0:
                result['original_price'] = original
        except Exception:
            pass

    # images
    gallery_key = next((k for k in keys1 if 'webGallery' in k), None)
    if gallery_key:
        try:
            gd = json.loads(page1_widget_states[gallery_key])
            imgs = gd.get('images', [])
            result['images'] = [
                {'url': _normalize_ozon_image_url(img['src']), 'is_primary': i == 0}
                for i, img in enumerate(imgs) if img.get('src')
            ]
            if result['images']:
                result['image_url'] = _to_wc_image_url(result['images'][0]['url'], 140)
        except Exception:
            pass

    # aspects / current variant metadata
    aspects_key = next((k for k in keys1 if 'webAspects' in k), None)
    if aspects_key:
        try:
            ad = json.loads(page1_widget_states[aspects_key])
            aspects = ad.get('aspects', []) or []
            spec_parts = []
            current_link = ""
            for aspect in aspects:
                variants = aspect.get("variants") or []
                current_variant = next(
                    (
                        v for v in variants
                        if str(v.get("sku") or "") == sku
                    ),
                    None,
                ) or next(
                    (
                        v for v in variants
                        if v.get("active") or v.get("isSelected")
                    ),
                    None,
                )
                if current_variant:
                    current_link = current_link or str(current_variant.get("link") or "")
                    searchable = (((current_variant.get("data") or {}).get("searchableText")) or "").strip()
                    if searchable:
                        spec_parts.append(searchable)
            if spec_parts:
                result["specifications"] = " / ".join(spec_parts)
            if current_link:
                result["link"] = current_link.split("?", 1)[0]
        except Exception:
            pass

    # ── PAGE 2 ──────────────────────────────────────────────
    keys2 = list(page2_widget_states.keys())

    # attributes from webCharacteristics
    char_key = next((k for k in keys2 if 'webCharacteristics' in k and 'pdpPage2column' in k), None)
    if char_key:
        try:
            cd = json.loads(page2_widget_states[char_key])
            attributes = []
            for group in cd.get('characteristics', []):
                for attr in group.get('short', []):
                    values = attr.get('values', [])
                    if values:
                        value = ', '.join(v.get('text', '') for v in values)
                        attributes.append({
                            'attribute_id': 0,
                            'key': attr.get('key', ''),
                            'name': attr.get('name', ''),
                            'value': value,
                        })
                        if attr.get('key') == 'Type':
                            result['typeNameRu'] = value
            result['attributes'] = attributes
        except Exception:
            pass

    # description + merge extra attributes from webDescription
    desc_keys = [k for k in keys2 if 'webDescription' in k and 'pdpPage2column' in k]
    for dk in desc_keys:
        try:
            dd = json.loads(page2_widget_states[dk])
            if not result['description']:
                desc = (dd.get('richAnnotation') or dd.get('annotation') or
                        dd.get('annotationShort') or dd.get('content') or
                        dd.get('description') or dd.get('text') or '')
                if desc:
                    result['description'] = desc
            chars = dd.get('characteristics', [])
            if chars:
                extra_attrs = [
                    {
                        'attribute_id': 0,
                        'key': c.get('title', ''),
                        'name': c.get('title', ''),
                        'value': c.get('content', ''),
                    }
                    for c in chars
                    if c.get('title') and c.get('content')
                ]
                result['attributes'] = _merge_attributes(result['attributes'], extra_attrs)
        except Exception:
            pass

    if not result["specifications"]:
        result["specifications"] = _build_specifications(result["attributes"])

    return result

# Page state classification
STATE_PRODUCT = "product"
STATE_ADULT = "adult_confirm"
STATE_SLIDER = "challenge_slider"
STATE_CHALLENGE = "challenge_wait"
STATE_BLOCKED = "blocked"
STATE_UNKNOWN = "unknown"


async def classify_page(page) -> str:
    """Classify the current page state."""
    try:
        url = page.url
        title = await page.title()
        title_lower = title.lower()
        obs = _page_observation(page)
        nav_age = time.monotonic() - float(obs.get("last_main_nav_at") or 0.0)

        # Hard block
        if "доступ ограничен" in title_lower or "access denied" in title_lower:
            body_text = await page.text_content("body") or ""
            body_lower = body_text.lower()
            has_access_limited_text = (
                "доступ ограничен" in body_lower
                or ("доступ" in body_lower and "огранич" in body_lower)
                or "access denied" in body_lower
            )
            has_incident = "инцидент" in body_lower or "incident" in body_lower
            if (
                obs.get("last_main_response_status") == 403
                and has_access_limited_text
                and has_incident
                and nav_age >= 1.5
            ):
                return STATE_BLOCKED
            return STATE_CHALLENGE

        adult_modal = await page.query_selector('[data-widget="userAdultModal"]')
        if adult_modal:
            return STATE_ADULT

        try:
            body_text = await page.text_content("body") or ""
        except Exception:
            body_text = ""
        body_lower = body_text.lower()
        if (
            "подтвердите возраст" in body_lower
            or "дату вашего рождения" in body_lower
            or "please indicate your date of birth" in body_lower
        ):
            return STATE_ADULT

        # Check for slider challenge
        slider = await page.query_selector(
            "[class*='slider'], [class*='captcha'], "\
            "[data-widget='slider'], div[id*='captcha']"\
        )
        if slider:
            return STATE_SLIDER

        # Antibot / challenge wait page (not yet final result)
        # Title-based: only match if title IS the challenge page title
        if any(k in title_lower for k in [
            "antibot captcha",
            "checking",
            "challenge",
            "проверка",
            "подождите",
            "подтвердите, что вы не бот",
        ]):
            return STATE_CHALLENGE

        # Ozon home/category page — treated as non-product but not blocked
        if "ozon.ru" in url and "/product/" not in url:
            return "home"

        # Content-based: use very specific markers only present on actual challenge pages
        content = await page.content()
        if any(k in content for k in ["cdn-cgi/challenge", "cf-browser-verification", "__cf_chl"]):
            return STATE_CHALLENGE
        # Ozon-specific antibot challenge page (not just the word in JSON bundles)
        if 'id="captcha-container"' in content or 'id="captcha"' in content:
            return STATE_SLIDER
        if (
            "Подтвердите, что вы не бот" in content
            or "Передвиньте ползунок, чтобы пазл попал в контур" in content
        ):
            return STATE_SLIDER

        # Product pages only after excluding captcha/challenge pages.
        if any(k in url for k in ["/product/", "/products/"]):
            name_el = await page.query_selector("h1")
            if name_el:
                return STATE_PRODUCT

        return STATE_UNKNOWN
    except Exception as e:
        log.debug(f"classify_page error: {e}")
        return STATE_UNKNOWN


async def extract_product(page) -> dict:
    """Extract product fields from a product page."""
    data = {}
    try:
        # Name
        h1 = await page.query_selector("h1")
        if h1:
            data["name"] = (await h1.inner_text()).strip()

        # Price — try multiple selectors Ozon uses (ordered by specificity)
        for sel in [
            "span.tsHeadline600Large",
            "span.tsHeadline500Medium",
            "[data-widget='webPrice'] span",
            "[class*='price-number']",
            "[class*='price_number']",
            "span[class*='Price']",
        ]:
            els = await page.query_selector_all(sel)
            for el in els:
                raw = (await el.inner_text()).strip()
                if raw and '₽' in raw and re.search(r'\d', raw):
                    data["price_raw"] = raw
                    nums = re.findall(r'[\d\s]+', raw.replace('\u2009', ''))
                    if nums:
                        data["price"] = int(nums[0].replace(' ', '').strip())
                    break
            if data.get("price"):
                break

        # Rating
        for sel in ["[data-widget='webRating'] span", "[class*='rating'] span"]:
            el = await page.query_selector(sel)
            if el:
                data["rating"] = (await el.inner_text()).strip()
                break

        # SKU from URL
        m = re.search(r"/(\d{7,})", page.url)
        if m:
            data["sku"] = m.group(1)

        data["url"] = page.url

        # Try __NEXT_DATA__ for richer structured data
        try:
            next_data = await page.evaluate("() => window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : null")
            if next_data:
                nd = json.loads(next_data)
                # Walk props.initialState for price/name if not found above
                state = nd.get("props", {}).get("initialState", {})
                if not data.get("name"):
                    # Try common paths
                    for path in [["seo", "title"], ["product", "name"]]:
                        v = state
                        for k in path:
                            v = v.get(k, {}) if isinstance(v, dict) else None
                        if v and isinstance(v, str):
                            data["name"] = v
                            break
        except Exception:
            pass

    except Exception as e:
        log.warning(f"extract_product error: {e}")

    return data
