"""Replay the immutable 22-test P28-I1 contract against the R1 core."""

import importlib.util
from pathlib import Path


def load_tests(loader, tests, pattern):
    original = Path(__file__).resolve().parent.parent / 'ПАКЕТ_P28_I1_2026_09_28' / 'test_route_core.py'
    spec = importlib.util.spec_from_file_location('p28_i1_original_tests', original)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return loader.loadTestsFromModule(module)
