from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_generator():
    path = Path("distribution/windows/generate_notices.py")
    spec = spec_from_file_location("parsezen_generate_notices", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_license_decoder_preserves_utf8_and_legacy_accents() -> None:
    generator = load_generator()

    assert generator._decode_license_text("Parsezen — código".encode()) == ("Parsezen — código")
    assert generator._decode_license_text("Jürgen ©".encode("cp1252")) == "Jürgen ©"
    assert generator._repair_upstream_replacement_glyphs("J\ufffdrgen Stenarson") == (
        "Jürgen Stenarson"
    )
