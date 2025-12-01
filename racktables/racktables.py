import re
from os.path import join

import requests
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth


class Racktables:
    def __init__(self, url, username, password) -> None:
        self.s = requests.Session()
        self.s.verify = "/etc/ssl/certs/SUSE_Trust_Root.pem"
        self.s.auth = HTTPBasicAuth(username, password)
        self.url = url

    def search(self, search_payload=None):
        if search_payload is None:
            search_payload = {}
        params = "&".join("{}={}".format(k, v) for k, v in search_payload.items())
        req = self.s.get(join(self.url, "index.php"), params=params)
        status = req.status_code
        if status == 401:
            msg = "Racktables returned 401 Unauthorized. Are your credentials correct?"
            raise Exception(msg)
        if status >= 300:
            msg = f"Racktables returned statuscode {status} while trying to access {req.request.url}. Manual investigation needed."
            raise Exception(msg)
        soup = BeautifulSoup(req.text, "html.parser")
        result_table = soup.find("table", {"class": "cooltable"})
        return result_table.find_all(
            "tr", lambda tag: tag is not None
        )  # Racktables does not use table-heads so we have to filter the header out (it has absolutely no attributes)


class RacktablesObject:
    def __init__(self, rt_obj) -> None:
        self.rt_obj = rt_obj

    def from_path(self, url_path) -> None:
        req = self.rt_obj.s.get(join(self.rt_obj.url, url_path))
        soup = BeautifulSoup(req.text, "html.parser")
        objectview_table = soup.find("table", {"class": "objectview"})
        portlets = list(objectview_table.find_all("div", {"class": "portlet"}))
        summary = next(filter(lambda x: x.find("h2").text == "summary", portlets))
        rows = list(summary.find_all("tr"))
        for row in rows:
            try:
                name = row.find("th").text
                value = row.find("td").text
                sane_name = re.sub(r"[^a-z_]+", "", name.lower().replace(" ", "_"))
                setattr(self, sane_name, value)
            except Exception:
                pass
