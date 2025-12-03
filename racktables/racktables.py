#!/usr/bin/env python3
from requests.auth import HTTPBasicAuth
from bs4 import BeautifulSoup
from os.path import join as join
import requests
import re


class Racktables:
    def __init__(self, url, username, password):
        self.s = requests.Session()
        self.s.verify = "/etc/ssl/certs/SUSE_Trust_Root.pem"
        self.s.auth = HTTPBasicAuth(username, password)
        self.url = url

    def search(self, search_payload={}):
        params = "&".join("%s=%s" % (k, v) for k, v in search_payload.items())
        req = self.s.get(join(self.url, "index.php"), params=params)
        status = req.status_code
        if status == 401:
            raise Exception("Racktables returned 401 Unauthorized. Are your credentials correct?")
        elif status >= 300:
            raise Exception(
                f"Racktables returned statuscode {status} while trying to access {req.request.url}. Manual investigation needed."
            )
        soup = BeautifulSoup(req.text, "html.parser")
        result_table = soup.find("table", {"class": "cooltable"})
        result_objs = result_table.find_all(
            "tr", lambda tag: tag is not None
        )  # Racktables does not use table-heads so we have to filter the header out (it has absolutely no attributes)
        return result_objs


class RacktablesObject:
    def __init__(self, rt_obj):
        self.rt_obj = rt_obj

    def from_path(self, url_path):
        req = self.rt_obj.s.get(join(self.rt_obj.url, url_path))
        soup = BeautifulSoup(req.text, "html.parser")
        objectview_table = soup.find("table", {"class": "objectview"})
        portlets = list(objectview_table.find_all("div", {"class": "portlet"}))
        summary = list(filter(lambda x: x.find("h2").text == "summary", portlets))[0]
        rows = list(summary.find_all("tr"))
        for row in rows:
            try:
                name = row.find("th").text
                value = row.find("td").text
                sane_name = re.sub(r"[^a-z_]+", "", name.lower().replace(" ", "_"))
                setattr(self, sane_name, value)
            except Exception:
                pass
