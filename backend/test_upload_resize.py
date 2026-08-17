"""端到端测试：上传图片自动缩放压缩（POST /cms/upload）"""
import io
import json
import time
import urllib.request
import urllib.error
from PIL import Image

BASE = "http://127.0.0.1:8000"
EMAIL, PASS = "platform@demo.com", "Platform@123"


def req(method, path, data=None, headers=None, raw=None, ctype=None):
    url = BASE + path
    body = None
    h = headers or {}
    if raw is not None:
        body = raw
        if ctype:
            h["Content-Type"] = ctype
    elif data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_ready(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s, _ = req("POST", "/auth/login",
                       {"email": EMAIL, "password": PASS})
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    if not wait_ready():
        print("[FAIL] 后端未就绪")
        return
    print("[OK] 后端就绪")

    # 登录
    s, b = req("POST", "/auth/login", {"email": EMAIL, "password": PASS})
    assert s == 200, (s, b)
    token = json.loads(b)["access_token"]
    print("[OK] 平台管理员登录成功")

    # 生成一张 3000x2000 的大 PNG（带噪点，文件较大）
    big = io.BytesIO()
    img = Image.new("RGB", (3000, 2000), (120, 160, 220))
    # 画一些色块增大体积
    for x in range(0, 3000, 200):
        for y in range(0, 2000, 200):
            img.paste(((x * 7) % 255, (y * 13) % 255, 100), (x, y, x + 100, y + 100))
    img.save(big, format="PNG")
    big_bytes = big.getvalue()
    print(f"[INFO] 原图: {img.size} -> {len(big_bytes)} bytes")

    # 上传大图
    boundary = b"----testboundary"
    body = (b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
            b"Content-Type: image/png\r\n\r\n" + big_bytes + b"\r\n--" + boundary + b"--\r\n")
    s, resp = req("POST", "/cms/upload", raw=body,
                  headers={"Authorization": f"Bearer {token}"},
                  ctype=f"multipart/form-data; boundary={boundary.decode()}")
    print("upload status:", s)
    out = json.loads(resp)
    print("upload resp:", out)
    assert s == 200, (s, resp)
    assert out["saved_bytes"] < out["original_bytes"], "未压缩!"
    assert out["ratio"] is not None and out["ratio"] < 1.0, "压缩比异常"

    # 校验保存后的图片宽度 <= 1920
    from urllib.request import urlopen
    url = BASE + out["url"]
    with urlopen(url, timeout=10) as r:
        saved = r.read()
    simg = Image.open(io.BytesIO(saved))
    print(f"[INFO] 保存图: {simg.size} -> {len(saved)} bytes")
    assert simg.width <= 1920, f"宽度超限 {simg.width}"
    print(f"[PASS] 大图上传成功：{img.width}->{simg.width}px, "
          f"{len(big_bytes)}->{len(saved)} bytes (ratio={out['ratio']})")

    # 小图（800x600）不应被放大
    small = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 100, 50)).save(small, format="PNG")
    sb = small.getvalue()
    body2 = (b"--" + boundary + b"\r\n"
             b'Content-Disposition: form-data; name="file"; filename="small.png"\r\n'
             b"Content-Type: image/png\r\n\r\n" + sb + b"\r\n--" + boundary + b"--\r\n")
    s2, resp2 = req("POST", "/cms/upload", raw=body2,
                    headers={"Authorization": f"Bearer {token}"},
                    ctype=f"multipart/form-data; boundary={boundary.decode()}")
    out2 = json.loads(resp2)
    with urlopen(BASE + out2["url"], timeout=10) as r:
        ssmall = Image.open(io.BytesIO(r.read()))
    print(f"[PASS] 小图(800x600)上传：保存宽度={ssmall.width}（应=800，不被放大）")
    assert ssmall.width == 800

    print("\n==== 全部通过 ====")


if __name__ == "__main__":
    main()
