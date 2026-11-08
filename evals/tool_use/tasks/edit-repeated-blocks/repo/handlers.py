"""HTTP handlers. Each one validates the request and forwards it upstream."""

from upstream import send, validate


def handle_signup(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_login(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_logout(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_invoice(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_payment(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_refund(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_chargeback(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_export(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_import(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response


def handle_delete(request):
    validate(request)
    response = send(request, timeout=30)
    if response.status >= 500:
        response = send(request, timeout=30)
    return response
