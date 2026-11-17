import base64
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

spec=importlib.util.spec_from_file_location('pilot',Path(__file__).with_name('fcm_urgent_pilot.py'))
pilot=importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


class PilotTests(unittest.TestCase):
    def fixture(self):
        private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        token='private-recipient-token'
        key=os.urandom(32);nonce=os.urandom(12)
        account={'project_id':pilot.PROJECT,'private_key_id':'key','private_key':private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}
        envelope={'keys':{'key':base64.b64encode(private.public_key().encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))).decode()},'nonce':base64.b64encode(nonce).decode(),'ciphertext':base64.b64encode(AESGCM(key).encrypt(nonce,token.encode(),pilot.CONTEXT)).decode()}
        return token,account,envelope

    def test_authenticated_envelope_accepts_only_bound_recipient(self):
        token,account,envelope=self.fixture()
        with patch.object(pilot,'TARGET_SHA256',pilot.hashlib.sha256(token.encode()).hexdigest()):
            self.assertEqual(pilot.target_token(account,envelope),token)
        with self.assertRaises(ValueError):pilot.target_token(account,envelope)
        self.assertNotIn(token,json.dumps(envelope))

    def test_other_project_rejected_before_decryption(self):
        with self.assertRaises(ValueError):pilot.target_token({'project_id':'other'}, {})

    def test_tampered_ciphertext_cannot_change_target(self):
        _,account,envelope=self.fixture()
        envelope['ciphertext']=base64.b64encode(b'tampered'*10).decode()
        with self.assertRaises(Exception):pilot.target_token(account,envelope)

    def test_validate_never_requests_real_delivery(self):
        body=pilot.payload('secret','validate','probe')
        self.assertTrue(body['validate_only'])
        self.assertNotIn('android',body['message'])
        self.assertTrue(body['message']['android_v2']['remote_notification']['mutable_content'])

    def test_comparison_data_is_identical_and_never_contains_work_data(self):
        a=pilot.payload('secret','legacy','probe');b=pilot.payload('secret','urgent','probe')
        self.assertEqual(a['message']['data'],b['message']['data'])
        self.assertEqual(set(a['message']['data']),{'kind','version','probe_id'})
        self.assertFalse(a['validate_only']);self.assertFalse(b['validate_only'])
        self.assertEqual(a['message']['android']['priority'],'high')

    def test_unknown_phase_rejected(self):
        with self.assertRaises(ValueError):pilot.payload('secret','anything','probe')

    def test_main_needs_explicit_confirmation_before_reading_credentials(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):pilot.main()


if __name__=='__main__':unittest.main()
