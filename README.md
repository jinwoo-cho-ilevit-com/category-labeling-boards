# category-labeling-boards

GT 복수정답 검수용 정적 사이트. 검수자별·카테고리별로 분리된 보드에서 판정/메모/GT 후보를
남기면 Supabase(`reviews` 테이블)에 자동 저장된다. GitHub Pages(public)로 배포된다.

- **공개 URL**: https://jinwoo-cho-ilevit-com.github.io/category-labeling-boards/
- **저장 백엔드**: Supabase `reviews` (`(reviewer, sample_id)` upsert, 무인증 publishable key)

## 구조

```
index.html                    # 검수자 선택 + 카테고리별/팀 진행률(Supabase 라이브) + 온보딩 링크
onboarding.html               # GT 복수정답 검수 온보딩 가이드 (자체완결)
manifest.json                 # 검수자·카테고리·슬러그·카테고리별 샘플 수
data/<rN>/<cN>.html           # 검수자 rN × 카테고리 cN 보드 (기존 뷰어 + 해당 카테고리 샘플만)
build_site.py                 # 아래 원본 보드 → 이 사이트를 생성하는 빌드 스크립트
```

- 슬러그: `rN`=검수자, `cN`=카테고리(진행 순서). 이름/라벨 매핑은 `manifest.json` 참고.
- 각 보드는 저장 경로를 Apps Script 대신 Supabase upsert로 사용하며 `localStorage`를 안전망으로 유지한다.

## 재생성 (원본 보드가 갱신됐을 때)

`build_site.py` 상단 상수를 확인/수정한 뒤 실행한다.

- `SRC_DIR`   : 원본 `board_<검수자>.html` 들이 있는 디렉터리
- `OUT_DIR`   : 이 저장소 경로
- `TARGET_ORDER` : 검수 대상 카테고리와 진행 순서 (목록 외 카테고리는 자동 제외)
- `SUPABASE_URL` / `SUPABASE_KEY` : 저장 대상 (publishable key)

```bash
python3 build_site.py     # data/ 를 매번 새로 생성, manifest.json / index.html 갱신
git add -A && git commit -m "chore: 보드 재생성" && git push
```

`build_site.py` 는 실행 시 `data/` 를 통째로 재생성하므로 구 슬러그가 잔존하지 않는다.
`.git` / `README.md` / `onboarding.html` / `build_site.py` 등 루트 자산은 건드리지 않는다.

## 운영

- **진행률 초기화**: Supabase에서 `truncate table public.reviews;`
- **접근 모델**: 무인증(publishable key 공개). URL을 아는 누구나 `reviews`에 쓰기 가능 —
  내부 검수용 신뢰 모델. 접근 제한이 필요하면 Supabase Auth로 전환.
