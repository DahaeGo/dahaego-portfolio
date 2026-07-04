#!/usr/bin/env python3
import base64
import http.server
import json
import os
import subprocess
import sys

PORT = 3131
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_JSON = os.path.join(BASE_DIR, "data", "projects.json")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_POST(self):
        if self.path == "/generate":
            self.handle_generate()
        elif self.path == "/publish":
            self.handle_publish()
        else:
            self.send_response(404)
            self.end_headers()

    # ══════════════════════════════════════════════════════════════
    # PUBLISH — 프로젝트 데이터 갱신 + 이미지 저장 + git commit/push
    # ══════════════════════════════════════════════════════════════
    def handle_publish(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._respond_error(400, "잘못된 요청입니다.")
            return

        project = data.get("project")
        new_images = data.get("newImages", [])

        if not project or not project.get("id"):
            self._respond_error(400, "프로젝트 정보가 없습니다.")
            return

        try:
            # 1. 새로 추가된 이미지 저장
            img_dir = os.path.join(BASE_DIR, "00_jpg")
            os.makedirs(img_dir, exist_ok=True)
            for img in new_images:
                filename = os.path.basename(img.get("filename", ""))
                if not filename:
                    continue
                img_bytes = base64.b64decode(img["data"])
                with open(os.path.join(img_dir, filename), "wb") as f:
                    f.write(img_bytes)
                print(f"[publish] 이미지 저장: {filename}")

            # 2. data/projects.json 갱신 (해당 프로젝트만 교체, 없으면 추가)
            os.makedirs(os.path.dirname(PROJECTS_JSON), exist_ok=True)
            if os.path.exists(PROJECTS_JSON):
                with open(PROJECTS_JSON, "r", encoding="utf-8") as f:
                    projects = json.load(f)
            else:
                projects = []

            idx = next((i for i, p in enumerate(projects) if p.get("id") == project["id"]), None)
            if idx is not None:
                projects[idx] = project
            else:
                projects.append(project)

            with open(PROJECTS_JSON, "w", encoding="utf-8") as f:
                json.dump(projects, f, ensure_ascii=False, indent=2)
                f.write("\n")

            print(f"[publish] projects.json 갱신: {project['id']}")

            # 3. git add / commit / push
            self._git_publish(project.get("name", project["id"]))

            self._respond_json({"ok": True})

        except subprocess.CalledProcessError as e:
            self._respond_error(500, f"git 오류: {e.output.strip() if e.output else str(e)}")
        except Exception as e:
            self._respond_error(500, str(e))

    def _git_publish(self, project_name):
        def run(args):
            return subprocess.run(
                args, cwd=BASE_DIR, capture_output=True, text=True, timeout=60
            )

        if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
            raise Exception("아직 GitHub 연동이 설정되지 않았어요. 처음 설정을 먼저 완료해주세요.")

        add = run(["git", "add", "-A"])
        if add.returncode != 0:
            raise Exception(f"git add 실패: {add.stderr.strip()}")

        commit = run(["git", "commit", "-m", f"Update: {project_name}"])
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            raise Exception(f"git commit 실패: {commit.stderr.strip() or commit.stdout.strip()}")

        push = run(["git", "push"])
        if push.returncode != 0:
            raise Exception(f"GitHub에 올리지 못했어요: {push.stderr.strip()}")

        print(f"[publish] git push 완료: {project_name}")

    # ══════════════════════════════════════════════════════════════
    # GENERATE — Claude CLI로 이미지 분석 + 텍스트 생성
    # ══════════════════════════════════════════════════════════════
    def handle_generate(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        name         = data.get("name", "")
        client       = data.get("client", "")
        date         = data.get("date", "")
        scope        = data.get("scope", "")
        contribution = data.get("contribution", "")
        image_paths  = data.get("imagePaths", [])

        image_lines = []
        for i, p in enumerate(image_paths):
            abs_p = os.path.join(BASE_DIR, p) if not os.path.isabs(p) else p
            role = "(히어로/메인)" if i == 0 else "(그리드 보조)" if i == 1 else f"(상세 {i+1})"
            image_lines.append(f"- {abs_p} {role}")
        image_section = "\n".join(image_lines) if image_lines else "(이미지 없음)"

        prompt = f"""너는 그래픽 디자이너 포트폴리오 카피라이터야. 아래 이미지 파일들을 Read 툴로 직접 열어서 확인한 뒤 포트폴리오 텍스트를 작성해줘.

이미지 파일 목록:
{image_section}

현재 프로젝트 정보:
- 프로젝트명: {name}
- Client: {client}
- Date: {date}
- Scope: {scope}
- Contribution: {contribution}

요청:
1. 프로젝트명 — 영문 대문자, 브랜드명 기반 (예: RE;CODE / MUSEUM PADO)
2. 카테고리 — 한 단어 영문 (예: Rebranding / Branding / Identity / Packaging)
3. 설명 — 3~4문장 한국어. 브랜드 소개 → 프로젝트 목적 → 디자인 방향 → 결과물 순서로.

반드시 아래 형식으로만 답해줘. 다른 말은 일절 하지 마:
프로젝트명:
카테고리:
설명: """

        print(f"[generate] {name} — 이미지 {len(image_paths)}개")

        # Find claude binary
        claude_bin = None
        candidates = [
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
            os.path.expanduser("~/.local/bin/claude"),
            os.path.expanduser("~/Library/Application Support/Claude/claude"),
        ]
        # also try PATH
        for path_dir in os.environ.get("PATH", "").split(":"):
            candidates.append(os.path.join(path_dir, "claude"))

        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                claude_bin = c
                break

        if not claude_bin:
            # fallback: try shell to find it
            try:
                result = subprocess.run(
                    ["bash", "-lc", "which claude"],
                    capture_output=True, text=True, timeout=10
                )
                found = result.stdout.strip()
                if found:
                    claude_bin = found
            except Exception:
                pass

        if not claude_bin:
            self._respond_error(500, "claude 바이너리를 찾을 수 없습니다. Claude Code가 설치되어 있는지 확인해주세요.")
            return

        try:
            result = subprocess.run(
                [claude_bin, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=BASE_DIR,
                env={**os.environ, "HOME": os.path.expanduser("~")},
            )
            output = result.stdout.strip()
            if not output and result.stderr:
                print("[stderr]", result.stderr[:300])
                output = result.stderr.strip()

            print("[result]", output[:150])
            self._respond_json({"result": output})

        except subprocess.TimeoutExpired:
            self._respond_error(500, "시간 초과 (120초). 다시 시도해주세요.")
        except Exception as e:
            self._respond_error(500, str(e))

    def _respond_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_error(self, code, msg):
        print(f"[error] {msg}")
        body = json.dumps({"error": msg, "ok": False}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print(f"✦ 다혜고 포트폴리오 서버 ready → http://localhost:{PORT}")
    print(f"  포트폴리오 폴더: {BASE_DIR}")
    print(f"  빌더:      http://localhost:{PORT}/builder.html")
    print(f"  사이트 미리보기: http://localhost:{PORT}/index.html")
    print(f"  종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버 종료")
        sys.exit(0)
