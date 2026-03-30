from collections.abc import Awaitable, Callable, Iterable
from typing import Any


def _page_url(page: Any) -> str:
    return str(getattr(page, "url", "") or "")


def _is_blank_page(page: Any) -> bool:
    return _page_url(page) in ("", "about:blank")


async def ensure_single_page(
    pages: Iterable[Any],
    create_page: Callable[[], Awaitable[Any]],
) -> Any:
    alive_pages = []
    for page in pages:
        try:
            if page.is_closed():
                continue
        except Exception:
            continue
        alive_pages.append(page)

    if not alive_pages:
        return await create_page()

    primary = next((page for page in alive_pages if not _is_blank_page(page)), alive_pages[0])
    for extra in alive_pages:
        if extra is primary:
            continue
        try:
            await extra.close()
        except Exception:
            pass
    return primary
