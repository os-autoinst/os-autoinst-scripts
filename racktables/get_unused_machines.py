#!/usr/bin/env python3
import os
from getpass import getpass

from racktables import Racktables, RacktablesObject

rt_url = os.environ.get("RT_URL", "https://racktables.suse.de")
user = os.environ["RT_USERNAME"] if "RT_USERNAME" in os.environ else input("Username: ")
pwd = os.environ["RT_PASSWORD"] if "RT_PASSWORD" in os.environ else getpass("Password (masked): ")

rt = Racktables(rt_url, user, pwd)
search_payload = {
    "andor": "and",
    "cft[]": "197",
    "cfe": "{%24typeid_4}+and+not+{Decommissioned}",
    "page": "depot",
    "tab": "default",
    "submit.x": "9",
    "submit.y": "24",
}
results = rt.search(search_payload)
for result_obj in results:
    url_path = result_obj.find("a")["href"]
    obj = RacktablesObject(rt)
    obj.from_path(url_path)
    try:
        print(obj.fqdn, flush=True)
    except Exception:
        print(obj.common_name, flush=True)
