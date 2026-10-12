# sandboxd는 명령을 bash -lc로 실행한다. 로그인 셸은 /etc/profile에서 PATH를 새로 정하므로
# 이미지 ENV의 사용자 설치 경로를 여기서 다시 붙인다.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
