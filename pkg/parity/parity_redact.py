"""Credential redaction for exports and backups."""


REDACTED_KEYS = frozenset({
    "gameyfin_password",
    "secret",       # webhook secret
    "api_key",
    "access_token",
})

def redact_settings(settings: dict) -> dict:
    """Return a shallow copy of settings with known credential fields removed."""
    if not isinstance(settings, dict):
        return {}
    
    cleaned = settings.copy()
    for key in REDACTED_KEYS:
        cleaned.pop(key, None)
        
    if "webhooks" in cleaned and isinstance(cleaned["webhooks"], list):
        new_webhooks = []
        for hook in cleaned["webhooks"]:
            if not isinstance(hook, dict):
                new_webhooks.append(hook)
                continue
            new_hook = hook.copy()
            secret = new_hook.pop("secret", None)
            new_hook["secret_set"] = bool(secret)
            new_webhooks.append(new_hook)
        cleaned["webhooks"] = new_webhooks
        
    return cleaned

def redact_state_for_export(state: dict) -> dict:
    """Return a deep-enough copy of state with credentials redacted from settings."""
    if not isinstance(state, dict):
        return {}
    
    cleaned = state.copy()
    if "settings" in cleaned:
        cleaned["settings"] = redact_settings(cleaned["settings"])
        
    return cleaned

def merge_preserved_credentials(restored_settings: dict, existing_settings: dict) -> dict:
    """After restoring a redacted backup, preserve existing local credentials."""
    if not isinstance(restored_settings, dict):
        restored_settings = {}
    if not isinstance(existing_settings, dict):
        existing_settings = {}
        
    merged = restored_settings.copy()
    
    for key in REDACTED_KEYS:
        if not merged.get(key) and existing_settings.get(key):
            merged[key] = existing_settings[key]
            
    if "webhooks" in merged and isinstance(merged["webhooks"], list):
        existing_hooks = {
            hook.get("id"): hook 
            for hook in existing_settings.get("webhooks", []) 
            if isinstance(hook, dict) and hook.get("id")
        }
        
        new_webhooks = []
        for hook in merged["webhooks"]:
            if not isinstance(hook, dict):
                new_webhooks.append(hook)
                continue
                
            hook_copy = hook.copy()
            hook_id = hook_copy.get("id")
            hook_copy.pop("secret_set", None)
            
            if not hook_copy.get("secret") and hook_id in existing_hooks:
                existing_secret = existing_hooks[hook_id].get("secret")
                if existing_secret:
                    hook_copy["secret"] = existing_secret
                    
            new_webhooks.append(hook_copy)
            
        merged["webhooks"] = new_webhooks
        
    return merged
