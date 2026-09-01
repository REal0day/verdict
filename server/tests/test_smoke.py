import base64, secrets, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")  # unused here

def test_crypto_roundtrip():
    from app import crypto
    blob = crypto.encrypt(b"hello world")
    assert crypto.decrypt(blob) == b"hello world"

def test_permissions_scoping():
    from app.permissions import visible_user_ids
    from app.models import Role
    class U: pass
    u = U(); u.role = Role.user; u.id = "u1"; u.team_id = None
    assert visible_user_ids(None, u) == ["u1"]
    a = U(); a.role = Role.admin; a.id = "a1"; a.team_id = None
    assert visible_user_ids(None, a) is None
