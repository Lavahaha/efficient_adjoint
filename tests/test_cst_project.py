"""cst_project：CST 工程文件的整份复制与内容检查。

背景（服务器实测踩坑）：CST 工程 = `<名字>.cst` 文件 + 同名文件夹
（外部结果目录），两者配套。只复制 .cst 会打开成空工程，CST 不报错。
"""

import zipfile

from eaopt.solver import cst_project


def _make_project(root, name="proj", with_companion=True, model=True):
    root.mkdir(parents=True, exist_ok=True)
    cst = root / f"{name}.cst"
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("history.xml", "<history>macro</history>")
        if model:
            z.writestr("model.xml", "<brick name='substrate'/>"
                                    "<port name='Port 1'/>")
        else:
            z.writestr("model.xml", "<brick/>")
    cst.write_bytes(buf.getvalue())
    if with_companion:
        sub = root / name
        (sub / "Result").mkdir(parents=True)
        (sub / "Result" / "S1,1.sig").write_text("data")
        (sub / "proj.lok").write_text("lock")
    return cst


def test_companion_dir_strips_suffix(tmp_path):
    assert cst_project.companion_dir(tmp_path / "a.cst") == tmp_path / "a"


def test_copy_project_brings_companion_dir_and_skips_lok(tmp_path):
    src = _make_project(tmp_path / "src")
    dst = cst_project.copy_project(src, tmp_path / "work", dst_name="fwd_proj.cst")
    assert dst == tmp_path / "work" / "fwd_proj.cst"
    assert dst.exists()
    # 同名文件夹连同内容一起搬过来，且跟着改名
    copied = tmp_path / "work" / "fwd_proj"
    assert (copied / "Result" / "S1,1.sig").read_text() == "data"
    assert not list(copied.rglob("*.lok"))          # 锁文件不复制
    # 原工程不动
    assert (tmp_path / "src" / "proj" / "proj.lok").exists()


def test_copy_project_without_companion_dir(tmp_path):
    src = _make_project(tmp_path / "src", with_companion=False)
    dst = cst_project.copy_project(src, tmp_path / "work")
    assert dst.read_bytes() == src.read_bytes()
    assert not (tmp_path / "work" / "proj").exists()


def test_copy_project_overwrite_removes_stale_companion(tmp_path):
    src = _make_project(tmp_path / "src")
    work = tmp_path / "work"
    stale = work / "proj" / "Result"
    stale.mkdir(parents=True)
    (stale / "old.sig").write_text("old")
    cst_project.copy_project(src, work)
    assert not (stale / "old.sig").exists()         # 旧内容被清掉，不混进来
    assert (work / "proj" / "Result" / "S1,1.sig").exists()


def test_describe_reports_model_inside_cst(tmp_path):
    src = _make_project(tmp_path / "src")
    lines = "\n".join(cst_project.describe(src))
    assert "zip 容器" in lines
    assert "substrate" in lines and "Port 1" in lines
    assert "模型数据在文件里" in lines
    assert "同名文件夹 proj/" in lines


def test_describe_flags_empty_project(tmp_path):
    src = _make_project(tmp_path / "src", with_companion=False, model=False)
    lines = "\n".join(cst_project.describe(src))
    assert "空工程" in lines
    assert "没有" in lines and "同名文件夹" in lines


def test_describe_missing_file(tmp_path):
    assert "不存在" in "\n".join(cst_project.describe(tmp_path / "nope.cst"))


def test_describe_handles_non_zip(tmp_path):
    p = tmp_path / "old.cst"
    p.write_bytes(b"\x00\x01 not a zip")
    assert "不是 zip 容器" in "\n".join(cst_project.describe(p))
