from pathlib import Path

from jd2021_installer.installers.autodance_processor import process_autodance_directory


def test_process_autodance_directory_copies_nested_non_ckd_assets(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "out"
    codename = "daddycool"

    autodance_dir = source / "cache" / "itf_cooked" / "x360" / "world" / "maps" / codename / "autodance"
    props_dir = autodance_dir / "props"
    props_dir.mkdir(parents=True, exist_ok=True)

    # Nested directory payload that previously triggered PermissionError when copied as file.
    (props_dir / "dummy.asset").write_bytes(b"payload")
    (props_dir / "skip_me.ckd").write_bytes(b"ignored")

    converted = process_autodance_directory(source, target, codename)

    copied_asset = target / "autodance" / "props" / "dummy.asset"
    skipped_ckd = target / "autodance" / "props" / "skip_me.ckd"

    assert converted >= 1
    assert copied_asset.exists()
    assert not skipped_ckd.exists()
