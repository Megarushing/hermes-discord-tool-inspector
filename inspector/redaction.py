"""Fail-closed persistence boundary; independent of Hermes redaction settings.

A supplied text redactor must implement an equivalent secret policy. Without one,
Hermes must be importable; missing or failing policy aborts capture, never falls
back to raw data. Pattern redaction cannot identify arbitrary unlabelled secrets.
"""
import json
import re

_SECRET = re.compile(r'password|passwd|secret|token|api.?key|authorization|cookie|credential|private.?key', re.I)


def forced_redactor(text):
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(text, force=True, redact_url_credentials=True)


def redact(value, text_redactor=None):
    policy = text_redactor or forced_redactor
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            original = str(key)
            safe_key = policy(original)
            if not isinstance(safe_key, str):
                raise TypeError('text redactor must return a string')
            unique = safe_key
            index = 2
            while unique in output:
                unique = f'{safe_key} [field {index}]'
                index += 1
            output[unique] = '[REDACTED]' if _SECRET.search(original) else redact(item, policy)
        return output
    if isinstance(value, (list, tuple)):
        return [redact(item, policy) for item in value]
    if isinstance(value, str):
        # Tool results are frequently JSON encoded inside a string. Decode first
        # so escaped/newline/nested credential fields cannot bypass the boundary.
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(redact(parsed, policy), ensure_ascii=True)
        result = policy(value)
        if not isinstance(result, str):
            raise TypeError('text redactor must return a string')
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError('tool records must contain JSON-compatible values')
