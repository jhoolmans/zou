import pyotp
import random
import string
import flask_bcrypt
from datetime import datetime, timedelta


from flask import request
from babel.dates import format_datetime

from flask_jwt_extended import get_jti
from ldap3 import Server, Connection, ALL, NTLM, SIMPLE
from ldap3.core.exceptions import (
    LDAPSocketOpenError,
    LDAPInvalidCredentialsResult,
)

from zou.app.services import persons_service
from zou.app.models.person import Person
from zou.app.services.exception import (
    EmailOTPAlreadyEnabledException,
    EmailOTPNotEnabledException,
    MissingOTPException,
    NoAuthStrategyConfigured,
    PersonNotFoundException,
    TooMuchLoginFailedAttemps,
    TOTPAlreadyEnabledException,
    TOTPNotEnabledException,
    TwoFactorAuthenticationNotEnabledException,
    UnactiveUserException,
    UserCantConnectDueToNoFallback,
    WrongOTPException,
    WrongPasswordException,
    WrongUserException,
)
from zou.app.stores import auth_tokens_store
from zou.app.utils import date_helpers, emails
from zou.app.models.person import Person

from sqlalchemy.orm.attributes import flag_modified


def check_auth(
    app,
    email,
    password,
    totp=None,
    email_otp=None,
    recovery_code=None,
    no_otp=False,
):
    """
    Depending on configured strategy, it checks if given email and password
    mach an active user in the database. It raises exceptions adapted to
    encountered error (no auth strategy configured, wrong email, wrong passwor
    or unactive user).
    App is needed as parameter to give access to configuration while avoiding
    cyclic imports.
    """
    if not email:
        raise WrongUserException()
    try:
        person = persons_service.get_person_by_email(email, unsafe=True)
    except PersonNotFoundException:
        try:
            person = persons_service.get_person_by_desktop_login(email)
        except PersonNotFoundException:
            raise WrongUserException()

    if not person.get("active", False):
        raise UnactiveUserException()

    login_failed_attemps = check_login_failed_attemps(person)

    strategy = app.config["AUTH_STRATEGY"]
    try:
        if strategy == "auth_local_classic":
            local_auth_strategy(person, password, app)
        elif strategy == "auth_local_no_password":
            no_password_auth_strategy(person, password, app)
        elif strategy == "auth_remote_ldap":
            ldap_auth_strategy(person, password, app)
        else:
            raise NoAuthStrategyConfigured()
    except WrongPasswordException:
        update_login_failed_attemps(
            person["id"], login_failed_attemps + 1, datetime.now()
        )
        raise WrongPasswordException()

    if not no_otp and person_two_factor_authentication_enabled(person):
        if not check_two_factor_authentication(
            person, totp, email_otp, recovery_code
        ):
            update_login_failed_attemps(
                person["id"], login_failed_attemps + 1, datetime.now()
            )
            raise WrongOTPException()

    if login_failed_attemps > 0:
        update_login_failed_attemps(person["id"], 0)

    if "password" in person:
        del person["password"]

    if "totp_secret" in person:
        del person["totp_secret"]

    if "email_otp_secret" in person:
        del person["email_otp_secret"]

    if "otp_recovery_codes" in person:
        del person["otp_recovery_codes"]

    return person


def no_password_auth_strategy(person, password, app):
    """
    No password auth strategy
    """
    return person


def local_auth_strategy(person, password, app=None):
    """
    Local strategy just checks that person and passwords are correct the
    traditional way (email is in database and related password hash corresponds
    to given password).
    Password hash comparison is based on BCrypt.
    """
    try:
        password_hash = person["password"] or ""
        if password_hash and flask_bcrypt.check_password_hash(
            password_hash, password
        ):
            return person
        else:
            raise WrongPasswordException()
    except ValueError:
        raise WrongPasswordException()


def ldap_auth_strategy(person, password, app):
    """
    Connect to an active directory server to know if given user can be
    authenticated.
    When person is not from ldap, it can try to connect with default auth
    strategy.
    (only if fallback is activated (via LDAP_FALLBACK flag) in configuration)
    """
    if person["is_generated_from_ldap"]:
        try:
            SSL = app.config["LDAP_SSL"]
            if app.config["LDAP_IS_AD_SIMPLE"]:
                user = "CN=%s,%s" % (
                    person["full_name"],
                    app.config["LDAP_BASE_DN"],
                )
                authentication = SIMPLE
            elif app.config["LDAP_IS_AD"]:
                user = "%s\%s" % (
                    app.config["LDAP_DOMAIN"],
                    person["desktop_login"],
                )
                authentication = NTLM
            else:
                user = "uid=%s,%s" % (
                    person["desktop_login"],
                    app.config["LDAP_BASE_DN"],
                )
                authentication = SIMPLE

            ldap_server = "%s:%s" % (
                app.config["LDAP_HOST"],
                app.config["LDAP_PORT"],
            )
            server = Server(ldap_server, get_info=ALL, use_ssl=SSL)
            conn = Connection(
                server,
                user=user,
                password=password,
                authentication=authentication,
                raise_exceptions=True,
            )
            conn.bind()
            return person

        except LDAPSocketOpenError:
            app.logger.error(
                "Cannot connect to LDAP/Active directory server %s "
                % (ldap_server)
            )
            raise LDAPSocketOpenError()

        except LDAPInvalidCredentialsResult:
            app.logger.error(
                "LDAP cannot authenticate user: %s" % person["email"]
            )
            raise WrongPasswordException()

    elif app.config["LDAP_FALLBACK"]:
        return local_auth_strategy(person, password, app)
    else:
        raise UserCantConnectDueToNoFallback()


def check_login_failed_attemps(person):
    """
    Checks that the person has not reached the failed login limit.
    """
    login_failed_attemps = person["login_failed_attemps"]
    if login_failed_attemps is None:
        login_failed_attemps = 0
    if (
        login_failed_attemps >= 5
        and date_helpers.get_datetime_from_string(person["last_login_failed"])
        + timedelta(minutes=1)
        > datetime.now()
    ):
        raise TooMuchLoginFailedAttemps()
    return login_failed_attemps


def update_login_failed_attemps(
    person_id, login_failed_attemps, last_login_failed=None
):
    """
    Update login failed attemps for a person_id.
    """
    person = Person.get(person_id)
    person.login_failed_attemps = login_failed_attemps
    if last_login_failed is not None:
        person.last_login_failed = last_login_failed
    person.commit()
    persons_service.clear_person_cache()
    return person.serialize()


def check_two_factor_authentication(
    person, totp=None, email_otp=None, recovery_code=None
):
    """
    Check multifactor authentication for a person.
    """
    if person["totp_enabled"] and totp is not None:
        return check_totp(person, totp)
    elif person["email_otp_enabled"] and email_otp is not None:
        return check_email_otp(person, email_otp)
    elif recovery_code is not None:
        return check_recovery_code(person, recovery_code)
    else:
        raise MissingOTPException(
            person["preferred_two_factor_authentication"],
            get_two_factor_authentication_enabled(person),
        )


def person_two_factor_authentication_enabled(person):
    return person["totp_enabled"] or person["email_otp_enabled"]


def person_two_factor_authentication_enabled_raw(person):
    return person.totp_enabled or person.email_otp_enabled


def get_two_factor_authentication_enabled(person):
    two_factor_authentication_enabled = ["recovery_code"]
    if person["totp_enabled"]:
        two_factor_authentication_enabled.append("totp")
    if person["email_otp_enabled"]:
        two_factor_authentication_enabled.append("email_otp")
    return two_factor_authentication_enabled


def remove_otp_revovery_code(person_id, recovery_hash):
    """
    Remove an otp recovery code for a person_id.
    """
    person = Person.get(person_id)
    person.otp_recovery_codes.remove(recovery_hash.encode())
    flag_modified(person, "otp_recovery_codes")
    person.commit()
    persons_service.clear_person_cache()
    return person.serialize()


def check_totp(person, totp):
    """
    Check TOTP for a person.
    """
    return pyotp.TOTP(person["totp_secret"]).verify(totp)


def check_email_otp(person, email_otp):
    """
    Check email OTP for a person.
    """
    count = auth_tokens_store.get("email-otp-count-%s" % person["email"])
    if count is not None:
        if pyotp.HOTP(person["email_otp_secret"]).verify(
            email_otp, int(count)
        ):
            auth_tokens_store.delete("email-otp-count-%s" % person["email"])
            return True
    return False


def check_recovery_code(person, recovery_code):
    """
    Check recovery code for a person.
    """
    for recovery_hash in person["otp_recovery_codes"]:
        if flask_bcrypt.check_password_hash(recovery_hash, recovery_code):
            remove_otp_revovery_code(person["id"], recovery_hash)
            return True
    return False


def pre_enable_totp(person_id):
    """
    Pre-enable TOTP for a person.
    """
    person = Person.get(person_id)
    if person.totp_enabled:
        raise TOTPAlreadyEnabledException()
    else:
        person.totp_secret = pyotp.random_base32()
        totp = pyotp.TOTP(person.totp_secret)
        organisation = persons_service.get_organisation()
        totp_provisionning_uri = totp.provisioning_uri(
            name=person.email, issuer_name="Kitsu %s" % organisation["name"]
        )
        person.totp_enabled = False
        person.commit()
        persons_service.clear_person_cache()
        return totp_provisionning_uri, person.totp_secret


def enable_totp(person_id, totp):
    """
    Enable TOTP for a person
    """
    person = Person.get(person_id)
    if person.totp_enabled:
        raise TOTPAlreadyEnabledException()
    elif check_totp(person.serialize(), totp):
        person.totp_enabled = True
        otp_recovery_codes = None
        if not person.otp_recovery_codes:
            otp_recovery_codes = generate_recovery_codes()
            person.otp_recovery_codes = hash_recovery_codes(otp_recovery_codes)
        if not person.preferred_two_factor_authentication:
            person.preferred_two_factor_authentication = "totp"
        person.commit()
        persons_service.clear_person_cache()
        return otp_recovery_codes
    else:
        raise WrongOTPException


def disable_totp(person_id):
    """
    Disable TOTP for a person.
    """
    person = Person.get(person_id)
    if not person.totp_enabled:
        raise TOTPNotEnabledException()
    person.totp_enabled = False
    person.totp_secret = None
    if not person_two_factor_authentication_enabled_raw(person):
        person.otp_recovery_codes = None
        person.preferred_two_factor_authentication = None
    elif person.preferred_two_factor_authentication == "totp":
        person.preferred_two_factor_authentication = "email_otp"
    person.commit()
    persons_service.clear_person_cache()
    return True


def pre_enable_email_otp(person_id):
    """
    Pre-enable mail OTP for a person.
    """
    person = Person.get(person_id)
    if person.email_otp_enabled:
        raise EmailOTPAlreadyEnabledException()
    else:
        person.email_otp_secret = pyotp.random_base32()
        person.email_otp_enabled = False
        person.commit()
        send_email_otp(person.serialize())
        persons_service.clear_person_cache()
        return True


def enable_email_otp(person_id, email_otp):
    """
    Enable mail OTP for a person
    """
    person = Person.get(person_id)
    if person.email_otp_enabled:
        raise EmailOTPAlreadyEnabledException()
    elif check_email_otp(person.serialize(), email_otp):
        person.email_otp_enabled = True
        otp_recovery_codes = None
        if not person.otp_recovery_codes:
            otp_recovery_codes = generate_recovery_codes()
            person.otp_recovery_codes = hash_recovery_codes(otp_recovery_codes)
        if not person.preferred_two_factor_authentication:
            person.preferred_two_factor_authentication = "email_otp"
        person.commit()
        persons_service.clear_person_cache()
        return otp_recovery_codes
    else:
        raise WrongOTPException


def disable_email_otp(person_id):
    """
    Disable email OTP for a person.
    """
    person = Person.get(person_id)
    if not person.email_otp_enabled:
        raise EmailOTPNotEnabledException()
    person.email_otp_enabled = False
    person.email_otp_secret = None
    if not person_two_factor_authentication_enabled_raw(person):
        person.otp_recovery_codes = None
        person.preferred_two_factor_authentication = None
    elif person.preferred_two_factor_authentication == "email_otp":
        person.person.preferred_two_factor_authentication = "totp"
    person.commit()
    persons_service.clear_person_cache()
    return True


def send_email_otp(person):
    """
    Send an email with OTP and store the OTP for checking after.
    """
    count = random.randint(0, 999999999999)
    otp = pyotp.HOTP(person["email_otp_secret"]).at(count)
    auth_tokens_store.add(
        "email-otp-count-%s" % person["email"], count, ttl=60 * 5
    )
    organisation = persons_service.get_organisation()
    time_string = format_datetime(
        datetime.utcnow(),
        tzinfo=person["timezone"],
        locale=person["locale"],
    )
    person_IP = request.headers.get("X-Forwarded-For", None)
    html = f"""<p>Hello {person["first_name"]},</p>

<p>
Your verification code is : <strong>{otp}</strong>
</p>

<p>
This one time password will expire after 5 minutes. After, you will have to request a new one.
This email was sent at this date : {time_string}.
The IP of the person who requested this is: {person_IP}.
</p>

Thank you and see you soon on Kitsu,
</p>
<p>
{organisation["name"]} Team
</p>
"""
    subject = f"{organisation['name']} Kitsu verification code : {otp}"
    emails.send_email(subject, html, person["email"])
    return True


def disable_two_factor_authentication_for_person(person_id):
    person = Person.get(person_id)
    if not person_two_factor_authentication_enabled_raw(person):
        raise TwoFactorAuthenticationNotEnabledException
    person.email_otp_enabled = False
    person.email_otp_secret = None
    person.totp_enabled = False
    person.totp_secret = None
    person.otp_recovery_codes = None
    person.preferred_two_factor_authentication = None
    person.commit()
    persons_service.clear_person_cache()
    return True


def generate_new_recovery_codes(person_id):
    """
    Generate new recovery codes for a person.
    """
    person = Person.get(person_id)
    otp_recovery_codes = generate_recovery_codes()
    person.otp_recovery_codes = hash_recovery_codes(otp_recovery_codes)
    flag_modified(person, "otp_recovery_codes")
    person.commit()
    persons_service.clear_person_cache()
    return otp_recovery_codes


def register_tokens(app, access_token, refresh_token=None):
    """
    Register access and refresh tokens to auth token store. That way they
    can be used like a session.
    """
    access_jti = get_jti(encoded_token=access_token)
    auth_tokens_store.add(
        access_jti, "false", app.config["JWT_ACCESS_TOKEN_EXPIRES"]
    )

    if refresh_token is not None:
        refresh_jti = get_jti(encoded_token=refresh_token)
        auth_tokens_store.add(
            refresh_jti, "false", app.config["JWT_REFRESH_TOKEN_EXPIRES"]
        )


def revoke_tokens(app, jti):
    """
    Remove access and refresh tokens from auth token store.
    """
    auth_tokens_store.add(jti, "true", app.config["JWT_ACCESS_TOKEN_EXPIRES"])


def is_default_password(app, password):
    """
    Check if password is default.
    """
    return (
        password == "default"
        and app.config["AUTH_STRATEGY"] != "auth_local_no_password"
    )


def generate_reset_token():
    """
    Generate and return a reset token.
    """
    return "".join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(64)
    )


def generate_recovery_codes():
    """
    Generate and return recovery codes.
    """
    return [
        "".join(
            random.SystemRandom().choice(
                string.ascii_uppercase + string.digits
            )
            for _ in range(10)
        )
        for _ in range(16)
    ]


def hash_recovery_codes(recovery_codes):
    """
    Hash recovery codes given as argument and return them.
    """
    return [
        flask_bcrypt.generate_password_hash(recovery_code)
        for recovery_code in recovery_codes
    ]


def check_login_failed_attemps(person):
    """
    Check login failed attemps for a person.
    """
    login_failed_attemps = person["login_failed_attemps"]
    if login_failed_attemps is None:
        login_failed_attemps = 0
    if (
        login_failed_attemps >= 5
        and date_helpers.get_datetime_from_string(person["last_login_failed"])
        + timedelta(minutes=1)
        > datetime.now()
    ):
        raise TooMuchLoginFailedAttemps()
    return login_failed_attemps
