import pytest

from planscout.security import TokenCipher, hash_password, verify_password


def test_password_hashing():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("anything", "not-a-hash")


def test_token_cipher_accepts_fernet_keys_and_host_generated_secrets():
    for key in (TokenCipher.generate_key(), "B0jr+hAPOY7pg92AN0c9MN4/eccz+Mdwnx4OkA1KFUk="):
        cipher = TokenCipher(key)
        assert cipher.decrypt(cipher.encrypt({"access_token": "abc"})) == {"access_token": "abc"}
    # The same secret always derives the same key, so tokens survive restarts.
    secret = "B0jr+hAPOY7pg92AN0c9MN4/eccz+Mdwnx4OkA1KFUk="
    assert TokenCipher(secret).decrypt(TokenCipher(secret).encrypt({"a": 1})) == {"a": 1}
    with pytest.raises(ValueError):
        TokenCipher("too-short")
