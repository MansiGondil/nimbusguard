from app.models import AuditLog
from app.security import (
    create_access_token,
    decode_token,
    decrypt_credentials,
    encrypt_credentials,
    hash_password,
    role_at_least,
    verify_password,
)
from app.services import audit


def test_password_hashing():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)


def test_jwt_roundtrip_and_roles():
    claims = decode_token(create_access_token("ops", "operator"))
    assert claims["sub"] == "ops" and claims["role"] == "operator"
    assert role_at_least("admin", "operator") and not role_at_least("viewer", "operator")


def test_credentials_encrypted_at_rest():
    token = encrypt_credentials({"snmp_community": "s3cret"})
    assert "s3cret" not in token
    assert decrypt_credentials(token) == {"snmp_community": "s3cret"}


async def test_audit_chain_detects_tampering(session):
    for i in range(3):
        await audit.record(session, "tester", "test.event", f"t{i}", {"n": i})
    assert (await audit.verify_chain(session))["valid"]
    entry = await session.get(AuditLog, 2)
    entry.detail = {"n": 999}
    await session.commit()
    result = await audit.verify_chain(session)
    assert not result["valid"] and result["broken_at"] == 2
