import requests


class Api():
    postal_code: str
    locale: str
    timeout_seconds: int
    session: requests.Session

    FLIP_API_URL = "https://backflipp.wishabi.com/flipp"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

    def __init__(self, postal_code: str, locale: str, timeout_seconds: int = 15):
        self.postal_code = postal_code
        self.locale = locale
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
        })

    def get_flyers(self):
        print("here")
        url = f"{self.FLIP_API_URL}/flyers"
        params = {
            "locale": self.locale,
            "postal_code": self.postal_code,
        }

        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return data

"""
GET /flipp/flyers?locale=en&postal_code=L1J3W8
  → list of flyers (filter by "Groceries" in categories)

GET /flipp/flyers/{flyer_id}
  → flyer detail WITH all items embedded

GET /flipp/items/{item_id}
  → individual item detail (from the original gist)

GET /flipp/items/search?q=chicken&postal_code=L1J3W8
  → keyword search across all flyers (noisy)
"""