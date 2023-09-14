import re


def _iter_resp_lines(resp):
    prev = ""
    for seg in resp.stream(amt=None, decode_content=False):
        if isinstance(seg, bytes):
            seg = seg.decode("utf8")
        seg = prev + seg
        lines = re.split("\\n", seg)
        if not lines[-1].endswith("\n"):
            prev = lines[-1]
            lines = lines[:-1]
        else:
            prev = ""
        for line in lines:
            yield line


import kubernetes

print("patching kubernetes.watch.watch.iter_resp_lines")
kubernetes.watch.watch.iter_resp_lines = _iter_resp_lines
print("patched kubernetes.watch.watch.iter_resp_lines 🙈")
