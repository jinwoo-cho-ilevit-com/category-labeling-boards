# category-labeling-boards

GT(정답) 복수정답 검수용 **정적 사이트**. 빌드 툴·프레임워크·패키지 매니저 없이 순수 HTML/JS로만
동작한다. 검수자별·카테고리별로 분리된 보드에서 판정/메모/GT 후보를 남기면 Supabase(`reviews`
테이블)에 upsert로 자동 저장되고, `index.html`이 이를 실시간 집계해 진행률을 보여준다.
GitHub Pages(public)로 배포된다.

- **공개 URL**: https://jinwoo-cho-ilevit-com.github.io/category-labeling-boards/
- **온보딩 가이드**: [`onboarding.html`](./onboarding.html) (GT 복수정답 검수 방법 · 자체완결)
- **저장 백엔드**: Supabase `reviews` — `(reviewer, sample_id)` upsert, 무인증 publishable key

## 구조

```
index.html                    # 검수자 선택 + 카테고리별/팀 진행률(Supabase 라이브) + 온보딩 링크
onboarding.html               # GT 복수정답 검수 온보딩 가이드 (자체완결)
manifest.json                 # 검수자·카테고리·슬러그·카테고리별 샘플 수
data/<rN>/<cN>.html           # 검수자 rN × 카테고리 cN 보드 (기존 뷰어 + 해당 카테고리 샘플만)
build_site.py                 # 원본 board_*.html → 이 사이트를 생성하는 빌드 스크립트
```

### 슬러그 규약

`rN` = 검수자(파일 정렬 순서), `cN` = 카테고리(`TARGET_ORDER` 순서). 이름/라벨 매핑은
`manifest.json`. 카테고리 매칭은 `norm()`(모든 공백 제거)로 정규화한다 — 라벨 표기는 원본,
매칭은 정규화값.

**검수자**

| 슬러그 | 이름   |
|:------:|:-------|
| `r1`   | 김민지 |
| `r2`   | 유다연 |
| `r3`   | 이지나 |
| `r4`   | 조승현 |

**카테고리** (진행 순서)

| 슬러그 | 라벨                     |
|:------:|:-------------------------|
| `c1`   | 2.1.1                    |
| `c2`   | 2.1.1 (출산/유아동)      |
| `c3`   | 2.1.1.2 (출산/유아동)    |
| `c4`   | 2.1.2 (출산/유아동)      |
| `c5`   | 2.2.1 (뷰티)             |
| `c6`   | 2.2.1 (영양제)           |

각 보드는 저장 경로로 Apps Script 대신 Supabase upsert(`syncPush`)를 사용하며, 저장 실패 시
`localStorage`를 안전망으로 유지한다.

## 재생성 워크플로 (핵심)

`data/`, `index.html`, `manifest.json`은 **생성 산출물**이다. 직접 손으로 편집하지 말고
`build_site.py`로 재생성한다. `main()`은 매 실행마다 `data/`를 통째로 삭제 후 재생성하므로
구 슬러그가 잔존하지 않는다.

```bash
python3 build_site.py     # data/ 재생성 + manifest.json / index.html 갱신
git add -A && git commit -m "chore(labeling): 보드 재생성" && git push
```

- 원본 `board_<검수자>.html`을 `SRC_DIR`(기본 `/Users/jwcho/Downloads/temp`)에 배치해야 실행된다.
  이 경로가 비어 있으면 `SystemExit`으로 중단된다 — **저장소만으로는 재생성 불가**.
- 산출물을 바꾸려면 `build_site.py` 상단 상수를 수정한다:
  - `TARGET_ORDER` — 검수 대상 카테고리 라벨 + 진행 순서. 목록에 없는 카테고리는 자동 제외.
    카테고리 추가/제거/순서변경은 반드시 여기서 한다.
  - `SUPABASE_URL` / `SUPABASE_KEY` — 저장 대상(publishable key).
  - `SRC_DIR` / `OUT_DIR` — 입력 원본 디렉터리 / 이 저장소 경로.
- **루트 자산은 건드리지 않는다**: `README.md`, `onboarding.html`, `build_site.py`, `.git`,
  `.nojekyll`, `CLAUDE.md`는 손대지 않고 `data/`만 재생성한다.

빌드 파이프라인은 원본 뷰어 template을 재사용해 `samples`만 카테고리별로 갈아끼우고,
`patch_html()`이 sync 로직을 정규식으로 잘라내 Supabase upsert + 네비게이션 바로 교체한다.
정규식 앵커를 못 찾으면 즉시 `RuntimeError`로 중단하므로, 원본 뷰어 구조가 바뀌면 앵커를 먼저
갱신해야 한다. (각 보드 파일은 임베드 데이터 때문에 ~28MB로 매우 크다.)

## 미리보기

빌드/린트/테스트는 없다. 정적 서버로 확인한다.

```bash
python3 -m http.server     # 이후 브라우저로 index.html 열기
```

## 저장 모델 (Supabase `reviews`)

- upsert 키: `on_conflict=reviewer,sample_id` (`Prefer: resolution=merge-duplicates`).
- 저장 컬럼: `reviewer, sample_id, grp, name, gt, tags`(`|` 조인), `url, note,
  gt_candidates`(`|` 조인).
- `index.html`은 로드 시 `reviewer, sample_id, grp`만 fetch해 검수자×카테고리 진행률을
  라이브 집계한다(먼저 0%로 렌더 후 갱신). fetch가 실패해도 메뉴는 사용 가능.

## 운영

- **진행률 초기화**: Supabase에서 `truncate table public.reviews;`
- **접근 모델**: 무인증(publishable key 공개). URL을 아는 누구나 `reviews`에 쓰기 가능한
  **내부 검수용 신뢰 모델**. 접근 제한이 필요하면 Supabase Auth로 전환.
