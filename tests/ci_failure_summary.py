"""Publish bounded JUnit failures as CI annotations, visible with check status."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import xml.etree.ElementTree as ET

path = Path(sys.argv[1])
if path.exists():
    for case in ET.parse(path).getroot().iter("testcase"):
        for child in case:
            if child.tag in {"failure", "error"}:
                detail = (child.text or child.get("message", ""))[-6000:]
                detail = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                print("::error title=" + case.get("name", "test failure") + "::" + detail)
