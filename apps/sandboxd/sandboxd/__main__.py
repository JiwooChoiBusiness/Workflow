import uvicorn

from sandboxd.app import app

PORT = 7000


def main() -> None:
    # 컨테이너는 sandbox-net에만 붙고 runner가 컨테이너 이름으로 찾아온다.
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
