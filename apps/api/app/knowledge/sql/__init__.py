"""Text2SQL(3단계 설계 10장). 서버는 sqlglot으로 검증하고 실행·준비는 샌드박스가 한다.

이 패키지는 DB·HTTP 계층을 모른다. 데이터 소스 행과 잇는 일은 app/services/sql_sources.py가 한다.
"""
