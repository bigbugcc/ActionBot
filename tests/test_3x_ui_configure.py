"""Check native cross-build configuration against pinned upstream source files."""

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/3x-ui"
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
# Fixtures are the unmodified upstream files at this commit, not patch constants:
# https://github.com/MHSanaei/3x-ui/tree/608ef4e4010bf83e2e78328e08f77efc051ad4d7
spec = importlib.util.spec_from_file_location("configure_3x_ui", ROOT / "bin/3x-ui/configure.py")
configurer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configurer)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("Dockerfile", "DockerInit.sh"):
            shutil.copyfile(FIXTURES / name, self.root / name)
        (self.root / "go.mod").write_text("module github.com/mhsanaei/3x-ui\n\ngo 1.27\n")
        config = self.root / "internal/config/config.go"
        config.parent.mkdir(parents=True)
        config.write_text("package config\nvar buildCommit string\nvar buildDate string\n")

    def configure(self, stable=True):
        with patch.dict(os.environ, {
            "STABLE_BUILD": "true" if stable else "false",
            "SOURCE_SHA": "0123456789abcdef0123456789abcdef01234567",
            "SOURCE_DATE": "2026-10-17T12:34:56Z",
        }):
            configurer.configure(self.root)
        return (self.root / "Dockerfile").read_text(encoding="utf-8")

    def stages(self, docker):
        starts = list(re.finditer(r"^FROM (.+)$", docker, re.M))
        return {
            match[1].rsplit(" AS ", 1)[-1] if " AS " in match[1] else "runtime":
                docker[match.start():starts[i + 1].start() if i + 1 < len(starts) else len(docker)]
            for i, match in enumerate(starts)
        }

    def test_compile_and_resources_run_on_build_platform_with_alpine_runtime(self):
        docker = self.configure()
        stages = self.stages(docker)
        self.assertIn("build-base", stages)
        base = stages["build-base"]
        self.assertRegex(base, r"^FROM --platform=\$BUILDPLATFORM golang:1\.27(?:-[\w.-]+)? AS build-base")
        self.assertNotIn("alpine", base.splitlines()[0])
        self.assertIn("COPY --from=actionbot cross-build.sh /usr/local/bin/3x-ui-cross-build", base)
        self.assertIn("COPY go.mod go.sum ./", base)
        self.assertIn("go mod download", base)
        self.assertNotIn("COPY . .", base)
        self.assertNotIn("ARG TARGETARCH", base)
        builder = stages["builder"]
        self.assertRegex(builder, r"^FROM build-base AS builder")
        for arg in ("BUILDARCH", "TARGETOS", "TARGETARCH", "TARGETVARIANT"):
            self.assertIn("ARG " + arg, builder)
        self.assertRegex(builder, r"3x-ui-cross-build install(?:\s|$)")
        self.assertRegex(builder, r"3x-ui-cross-build build(?:\s|$)")
        self.assertIn("COPY --from=frontend /src/internal/web/dist ./internal/web/dist", builder)
        resources = stages["resources"]
        self.assertRegex(resources, r"^FROM build-base AS resources")
        self.assertIn("ARG TARGETARCH", resources)
        self.assertIn("ARG TARGETVARIANT", resources)
        self.assertIn('./DockerInit.sh "$TARGETARCH" "$TARGETVARIANT"', resources)
        self.assertNotIn("3x-ui-cross-build install", resources)
        self.assertNotIn("FROM builder", resources)
        runtime = stages["runtime"]
        self.assertRegex(runtime, r"^FROM alpine(?:\s|$)")
        self.assertIn("COPY --from=builder /app/build/x-ui /app/x-ui", runtime)
        self.assertIn("COPY --from=resources /app/build/ /app/", runtime)
        self.assertIn("ENV TZ=Asia/Shanghai", runtime)
        for path in ("DockerEntrypoint.sh", "x-ui.sh", "internal/web/translation"):
            self.assertIn("COPY --from=builder /app/" + path, runtime)

    def test_resource_failures_are_visible_and_have_a_stage_exit_record(self):
        self.configure()
        init = (self.root / "DockerInit.sh").read_text()
        # Run only the prelude and architecture mapping, with no network/writes.
        script = init.split("MTG_MULTI_VER=", 1)[0] + "\nexit 42\n"
        result = subprocess.run([BASH, "-c", script, "DockerInit.sh", "arm", "v6"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 42, result.stderr)
        for field in ("stage=resources", "platform=arm/v6", "exit_code=42", "duration_s="):
            self.assertIn(field, result.stderr)
        self.assertIn("curl --show-error --retry", init)

    def test_upstream_go_version_is_preserved_when_selecting_native_debian_image(self):
        path = self.root / "Dockerfile"
        path.write_text(path.read_text().replace("golang:1.27-alpine", "golang:1.28.1-alpine"))
        docker = self.configure()
        self.assertRegex(docker, r"FROM --platform=\$BUILDPLATFORM golang:1\.28\.1(?:-[\w.-]+)? AS build-base")
        self.assertNotIn("golang:1.27", docker)

    def test_stable_build_keeps_upstream_linker_flags(self):
        docker = self.configure()
        self.assertIn('3x-ui-cross-build build "-w -s"', docker)
        self.assertNotIn("internal/config.buildCommit", docker)
        self.assertNotIn("internal/config.buildDate", docker)

    def test_numeric_alpine_tag_and_equivalent_package_whitespace_are_supported(self):
        path = self.root / "Dockerfile"
        docker = path.read_text().replace("golang:1.27-alpine", "golang:1.28.1-alpine3.23")
        docker = docker.replace("  build-base", "\tbuild-base").replace("  gcc", "    gcc")
        path.write_text(docker)
        self.assertIn("golang:1.28.1 AS build-base", self.configure())

    def test_staging_or_second_replace_failure_preserves_both_originals(self):
        paths = [self.root / name for name in ("Dockerfile", "DockerInit.sh")]
        before = {path: path.read_bytes() for path in paths}
        for operation, fail_at in (("stage", 1), ("stage", 2), ("stage", 3), ("stage", 4), ("replace", 2)):
            with self.subTest(operation=operation, fail_at=fail_at):
                calls = 0
                owner = configurer if operation == "stage" else configurer.os
                method = "stage_file" if operation == "stage" else "replace"
                real_operation = getattr(owner, method)

                def fail_operation(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("injected write failure")
                    return real_operation(*args, **kwargs)

                with patch.object(owner, method, side_effect=fail_operation):
                    with self.assertRaisesRegex(OSError, "injected"):
                        self.configure()
                self.assertEqual({path: path.read_bytes() for path in paths}, before)
                self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                                 ["DockerInit.sh", "Dockerfile", "go.mod", "internal"])

    def test_rollback_failure_retains_and_reports_original_backup(self):
        path = self.root / "Dockerfile"
        original = path.read_bytes()
        real_replace = os.replace
        calls = 0

        def fail_commit_and_rollback(*args):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("injected failure")
            return real_replace(*args)

        with patch.object(configurer.os, "replace", side_effect=fail_commit_and_rollback):
            with self.assertRaisesRegex(RuntimeError, "original retained at") as error:
                self.configure()
        backups = list(self.root.glob(".*.actionbot-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertIn(str(backups[0]), str(error.exception))

    def test_dev_build_stamps_source_revision_and_commit_date(self):
        docker = self.configure(stable=False)
        self.assertIn("-X github.com/mhsanaei/3x-ui/internal/config.buildCommit=01234567", docker)
        self.assertIn("-X github.com/mhsanaei/3x-ui/internal/config.buildDate=2026-10-17T12:34:56Z", docker)
        self.assertIn("-w -s", docker)

    def test_five_platforms_select_matching_xray_and_mtg_resources(self):
        self.configure()
        init = (self.root / "DockerInit.sh").read_text()
        # Execute only the architecture selection, before any network or writes.
        selection = init.split("MTG_MULTI_VER=", 1)[0]
        mtg = re.search(r"case \$FNAME in\n.*?\nesac", init, re.S)
        self.assertIsNotNone(mtg)
        script = selection + mtg[0] + '\nprintf "%s|%s|%s" "$ARCH" "$FNAME" "$MTGARCH"\n'
        for arch, variant, expected in (
            ("amd64", "", "64|amd64|amd64"),
            ("arm64", "", "arm64-v8a|arm64|arm64"),
            ("386", "", "32|386|386"),
            ("arm", "v6", "arm32-v6|arm32|armv6"),
            ("arm", "v7", "arm32-v7a|arm32|armv7"),
        ):
            with self.subTest(arch=arch, variant=variant):
                result = subprocess.run([BASH, "-c", script, "DockerInit.sh", arch, variant],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
        for arch, variant in (("mips", ""), ("arm", "v5")):
            with self.subTest(arch=arch, variant=variant):
                result = subprocess.run([BASH, "-c", script, "DockerInit.sh", arch, variant],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsupported", result.stderr)

    def test_unknown_upstream_structure_fails_before_writing_either_file(self):
        docker = (FIXTURES / "Dockerfile").read_text()
        init = (FIXTURES / "DockerInit.sh").read_text()
        mutations = (
            ("Dockerfile", docker.replace("  unzip", "  unzip bash")),
            ("Dockerfile", docker.replace("golang:1.27-alpine", "golang:1.27-alpine-edge")),
            ("Dockerfile", docker.replace("FROM golang:1.27-alpine AS builder", "FROM unknown AS builder")),
            ("Dockerfile", docker.replace("COPY . .", "COPY ./ ./")),
            ("Dockerfile", docker.replace('RUN go build -ldflags "-w -s"', 'RUN go build -tags custom -ldflags "-w -s"')),
            ("Dockerfile", docker.replace('RUN ./DockerInit.sh "$TARGETARCH"', 'RUN ./DownloadResources.sh "$TARGETARCH"')),
            ("Dockerfile", docker.replace("COPY --from=builder /app/build/ /app/", "COPY --from=builder /app/output/ /app/")),
            ("DockerInit.sh", init.replace('FNAME="i386"', 'FNAME="x86"')),
        )
        for name, changed in mutations:
            with self.subTest(name=name, changed=changed[:80]):
                for source_name in ("Dockerfile", "DockerInit.sh"):
                    shutil.copyfile(FIXTURES / source_name, self.root / source_name)
                (self.root / name).write_text(changed)
                before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
