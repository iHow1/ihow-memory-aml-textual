"""Validation metadata that never retains request values or exception messages."""
import json
import logging

LOGGER = logging.getLogger('ihow_memory.validation')


def metadata(field, error_type, value):
    # Callers supply fixed field paths, never client-provided field names.
    kind = {str: 'string', list: 'array', dict: 'object', int: 'integer',
            float: 'number', bool: 'boolean', bytes: 'bytes', type(None): 'null'}.get(type(value), 'unknown')
    length = len(value) if type(value) in (str, list, dict, bytes) else None
    return {'field': field, 'error_type': error_type, 'type': kind, 'length': length}


class InvalidInput(ValueError):
    def __init__(self, message, field, error_type, value):
        super().__init__(message)
        self.diagnostic = metadata(field, error_type, value)


def log_rejection(error, value, field='payload', error_type='validation_error'):
    diagnostic = (error.diagnostic if isinstance(error, InvalidInput)
                  else metadata(field, error_type, value))
    LOGGER.warning(json.dumps(diagnostic, ensure_ascii=True, allow_nan=False))


def log_tolerance(field, error_type, value, *, count=None):
    diagnostic = metadata(field, error_type, value)
    if count is not None:
        # Unknown fields expose only their count, never their names or values.
        diagnostic['length'] = count
    LOGGER.warning(json.dumps(diagnostic, ensure_ascii=True, allow_nan=False))
