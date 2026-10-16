"""Caller-owned execution limits; absence never means a default deadline."""
from .errors import caller_error


def timeout_value(value, name="timeout_ms"):
    if value is not None and (type(value) is not int or value <= 0):
        raise caller_error(f"{name} must be a positive integer when explicitly supplied")
    return value


def execution_timeout(arguments):
    values = []
    for key in ("timeout_ms", "timeout"):
        if key in arguments:
            value = arguments[key]
            if value is None:
                raise caller_error(f"omit {key} for an operation without an execution deadline")
            values.append(timeout_value(value, key))
    if len(values) == 2 and values[0] != values[1]:
        raise caller_error("timeout_ms and timeout must agree when both are supplied")
    return values[0] if values else None
