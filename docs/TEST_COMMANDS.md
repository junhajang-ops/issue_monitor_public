# 테스트 명령어 치트시트

`test_all_cases.py`로 시나리오, 변형, 반복 횟수를 골라 실행합니다.

## 기본 구조

```powershell
python test_all_cases.py [케이스...] -r [반복횟수] --variants [변형...]
```

## 케이스 목록

| ID | 내용 |
|---|---|
| `c01` | 서버/접속 장애 true |
| `c02` | 서버/접속 장애 false |
| `c03` | 결제 문제 true |
| `c04` | 결제 문제 false |
| `c05` | 계정 문제 true |
| `c06` | 계정 문제 false |
| `c07` | 운영 리스크 1명 모호 false |
| `c08` | 운영 리스크 2명 동일 버그 true |
| `c09` | 운영 리스크 2명 모호 동일 현상 true |
| `c10` | 일반 대화 false |

## 변형 목록

| ID | 배치 |
|---|---|
| `v1` | 문제 메시지 초반 배치 |
| `v2` | 문제 메시지 중반 배치 |
| `v3` | 문제 메시지 후반 배치 |
| `v4` | 문제 메시지 분산 배치 |

## 자주 쓰는 명령어

전체 케이스, 전체 변형, 3회 반복:

```powershell
python test_all_cases.py
```

C08만 전체 변형 3회:

```powershell
python test_all_cases.py c08
```

C08만 1회:

```powershell
python test_all_cases.py c08 -r 1
```

C08의 v3만 1회:

```powershell
python test_all_cases.py c08 -r 1 --variants v3
```

C08의 v3, v4만 3회:

```powershell
python test_all_cases.py c08 --variants v3 v4
```

C08, C09만 전체 변형 3회:

```powershell
python test_all_cases.py c08 c09
```

C03, C04 결제 케이스만 v1/v3 1회:

```powershell
python test_all_cases.py c03 c04 -r 1 --variants v1 v3
```

## Slack 주의

현재 `.env`가 아래처럼 되어 있으면 테스트 결과가 false 포함 전부 Slack으로 전송됩니다.

```env
SLACK_NOTIFY_ALL=1
SLACK_NOTIFY_TESTS=1
```

처음 확인할 때는 작은 범위로 실행하세요.

```powershell
python test_all_cases.py c08 -r 1 --variants v3
```
