"""On-demand, credential-free views of Hermes's own provider/model inventory.

No discovery at registration time and no second provider/credential registry in this plugin.
"""
import json

from .teammates_config import MODEL_ID, PROVIDER_ID


def _native_rows():
    from hermes_cli.inventory import load_picker_context
    from hermes_cli.model_switch import list_authenticated_providers
    context = load_picker_context()
    rows = list_authenticated_providers(
        current_provider=context.current_provider, current_model=context.current_model,
        current_base_url=context.current_base_url, user_providers=context.user_providers,
        custom_providers=context.custom_providers, excluded_providers=context.excluded_providers or [],
        non_blocking_catalogs=True, probe_custom_providers=False, probe_current_custom_provider=False,
        for_picker=True)
    return rows


def _metadata(provider, model):
    """Whitelist brief, known catalog metadata; never forward a provider's raw record."""
    try:
        from agent.models_dev import get_model_info
        info = get_model_info(provider, model)
        if info is None:
            return {}
        return dict(name=str(info.name)[:100], context_window=info.context_window,
                    supports_tools=info.tool_call, supports_reasoning=info.reasoning,
                    supports_vision=info.supports_vision(), metadata_source='Hermes/models.dev (may be incomplete)')
    except Exception:
        return {}


def discover(provider=None, query=None, limit=25, offset=0):
    if ((provider is not None and (not isinstance(provider, str) or not PROVIDER_ID.fullmatch(provider))) or
            (query is not None and (not isinstance(query, str) or len(query) > 200)) or
            type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0):
        return dict(ok=False, error='invalid_discovery',
                    message='Use a provider ID, query of at most 200 characters, integer limit 1-100 and offset >= 0')
    try:
        rows = _native_rows()
    except Exception as exc:
        # Provider failures can contain credential-bearing URLs/headers: never echo their message.
        return dict(ok=False, error='catalog_unavailable', message=f'Hermes inventory unavailable ({type(exc).__name__})')
    providers = {}
    for row in rows:
        key = row.get('slug')
        if not isinstance(key, str) or not PROVIDER_ID.fullmatch(key):
            continue
        entry = providers.setdefault(key, dict(name=str(row.get('name') or key)[:100], models=set()))
        entry['models'].update(model for model in (row.get('models') or [])
                               if isinstance(model, str) and MODEL_ID.fullmatch(model))
    if provider is not None and provider not in providers:
        return dict(ok=False, error='unknown_provider', message='provider is not in this profile’s Hermes inventory')
    if provider is None and not query:
        kind = 'providers'
        entries = [dict(provider=key, name=row['name'], model_count=len(row['models']))
                   for key, row in sorted(providers.items())]
    else:
        kind = 'models'
        pairs = {(key, model) for key, row in providers.items() if provider is None or key == provider
                 for model in row['models'] if not query or query.casefold() in model.casefold()}
        entries = [dict(provider=key, model=model) for key, model in sorted(pairs)]
    page = [dict(entry) for entry in entries[offset:offset + limit]]
    if kind == 'models':
        for entry in page:
            entry.update(_metadata(entry['provider'], entry['model']))
    result = dict(ok=True, **{kind: page}, total=len(entries), offset=offset, limit=limit,
                  note='Best-known Hermes catalogs (cached/curated), not an access guarantee. '
                       'Exact IDs may work even when missing here. Preferences can live in prompts/AGENTS.md.')
    while True:
        more = offset + len(page) < len(entries)
        result.update(has_more=more, next_offset=offset + len(page) if more else None)
        if len(json.dumps(result, ensure_ascii=False)) <= 16000 or not page:
            return result
        page.pop()
