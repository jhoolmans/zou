from flask import Blueprint
from zou.app.utils.api import configure_api_from_blueprint

from .resources import (
    LoginResource,
    LogoutResource,
    AuthenticatedResource,
    ChangePasswordResource,
    RegistrationResource,
    RefreshTokenResource,
    ResetPasswordResource,
    TOTPResource,
    EmailOTPResource,
    RecoveryCodesResource,
)

routes = [
    ("/auth/login", LoginResource),
    ("/auth/logout", LogoutResource),
    ("/auth/authenticated", AuthenticatedResource),
    ("/auth/register", RegistrationResource),
    ("/auth/change-password", ChangePasswordResource),
    ("/auth/reset-password", ResetPasswordResource),
    ("/auth/refresh-token", RefreshTokenResource),
    ("/auth/totp", TOTPResource),
    ("/auth/email-otp", EmailOTPResource),
    ("/auth/recovery-codes", RecoveryCodesResource),
]

blueprint = Blueprint("auth", "auth")
api = configure_api_from_blueprint(blueprint, routes)
