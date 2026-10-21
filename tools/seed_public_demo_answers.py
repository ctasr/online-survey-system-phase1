"""Register distinct demo accounts and submit real answers through the public JSON API.

This intentionally adds six responses to the two published experience surveys.
Run once with --apply; no passwords are printed or stored.
"""

import json
import secrets
import sys
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ORIGIN = "https://119.28.27.184"
PLANS = {
    "JbI0U8Nm": [
        {
            "你更想参加哪类周末活动？": "室内活动",
            "室内活动中你更喜欢哪一种？": "电影",
            "你有兴趣参加哪些活动？": ["电影", "桌游"],
            "一个月大约能参加几次？": 2,
            "你偏好的活动时间或地点是？": "周六下午，市区交通方便的地方",
            "还有什么建议？": "希望提前公布活动时间。",
        },
        {
            "你更想参加哪类周末活动？": "户外活动",
            "户外活动中你更喜欢哪一种？": "骑行",
            "你有兴趣参加哪些活动？": ["骑行", "徒步"],
            "一个月大约能参加几次？": 4,
            "你偏好的活动时间或地点是？": "周日上午，公园或绿道",
            "还有什么建议？": "希望提供集合地点和交通指引。",
        },
        {
            "你更想参加哪类周末活动？": "户外活动",
            "户外活动中你更喜欢哪一种？": "徒步",
            "你有兴趣参加哪些活动？": ["暂时不参加"],
            "还有什么建议？": "近期时间不合适，以后可以再参加。",
        },
    ],
    "HQriIaHd": [
        {
            "你目前是否正在学习这门课程？": "是",
            "你主要通过哪些方式学习？": ["课堂听讲", "阅读教材"],
            "你每周用于这门课程的课外学习时间（小时）？": 5,
            "哪种学习方式对你最有帮助？": "课堂讲解配合练习",
            "你是否参加过本课程的小测？": "是",
            "最近一次小测得分是多少？": 86,
            "希望课程在哪方面改进？": "希望增加更多实际案例和课后答疑。",
            "你最希望增加哪两种学习支持？": ["例题讲解", "练习反馈"],
        },
        {
            "你目前是否正在学习这门课程？": "还没有",
            "你是否参加过本课程的小测？": "还没有",
            "希望课程在哪方面改进？": "希望开课前提供学习计划和复习资料。",
            "你最希望增加哪两种学习支持？": ["课堂答疑", "复习资料"],
        },
        {
            "你目前是否正在学习这门课程？": "是",
            "你主要通过哪些方式学习？": ["录播视频", "小组讨论"],
            "你每周用于这门课程的课外学习时间（小时）？": 1,
            "你是否参加过本课程的小测？": "是",
            "最近一次小测得分是多少？": 73,
            "希望课程在哪方面改进？": "希望把录播内容分成更短的小节。",
            "你最希望增加哪两种学习支持？": ["课堂答疑", "练习反馈"],
        },
    ],
}


class ApiSession:
    def __init__(self):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = None

    def call(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["X-CSRF-Token"] = self.csrf
        request = Request(ORIGIN + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error


def main():
    if sys.argv[1:] != ["--apply"]:
        raise SystemExit("This creates six real demo accounts and responses. Pass --apply to run.")
    batch = secrets.token_hex(3)
    created = 0
    for slug, plans in PLANS.items():
        for number, plan in enumerate(plans, 1):
            session = ApiSession()
            _, csrf_data = session.call("GET", "/api/v1/csrf")
            session.csrf = csrf_data["csrf_token"]
            username = f"演示答题_{batch}_{number}_{slug[:2]}"
            password = secrets.token_urlsafe(8)
            status, _ = session.call("POST", "/api/v1/auth/register", {
                "username": username, "password": password, "confirmation": password,
            })
            assert status == 201
            _, login = session.call("POST", "/api/v1/auth/login", {
                "username": username, "password": password,
            })
            session.csrf = login["csrf_token"]
            _, public = session.call("GET", f"/api/v1/public/surveys/{slug}")
            survey = public["survey"]
            identifiers = {question["title"]: question["id"] for question in survey["questions"]}
            assert set(plan) <= set(identifiers), (slug, set(plan) - set(identifiers))
            answers = {identifiers[title]: value for title, value in plan.items()}
            _, assessment = session.call("POST", f"/api/v1/public/surveys/{slug}/assess", {
                "answers": answers,
            })
            assert not assessment["errors"], (slug, assessment)
            assert set(assessment["reachable"]) == set(answers), (slug, assessment)
            status, submission = session.call("POST", f"/api/v1/public/surveys/{slug}/responses", {
                "revision": survey["revision"], "identity_mode": "named", "answers": answers,
            })
            assert status == 201
            created += 1
            print(f"PASS {slug}: account {username}, response {submission['response_id']}")
    print(f"Created {created} demo accounts and {created} real responses via {ORIGIN}")


if __name__ == "__main__":
    main()
