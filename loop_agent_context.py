from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContextFile:
    path: str
    absolute_path: Path
    size_bytes: int
    content: str


@dataclass(frozen=True)
class ContextBatch:
    index: int
    files: tuple[ContextFile, ...]
    text: str
    size_bytes: int


@dataclass
class ContextResult:
    success: bool
    batches: list[ContextBatch]
    files: list[ContextFile]
    skipped_files: list[str]
    total_bytes: int
    total_context_bytes: int
    total_characters: int
    error: str | None = None


@dataclass
class ContextConfig:
    enabled: bool = True
    root_dir: str = "."
    include_dirs: list[str] | None = None
    extensions: list[str] | None = None

    batch_limit_kb: int = 20
    max_files: int = 100

    encoding: str = "utf-8"
    include_manifest: bool = True
    include_file_metadata: bool = True
    sort_files: bool = True

    @classmethod
    def from_file(cls, config_path: str | Path) -> "ContextConfig":
        config_path = Path(config_path)

        parser = configparser.ConfigParser()
        loaded = parser.read(config_path, encoding="utf-8")

        if not loaded:
            raise FileNotFoundError(
                f"Context config file not found: {config_path}"
            )

        section = parser["context"]

        include_dirs = [
            item.strip()
            for item in section.get("include_dirs", "").splitlines()
            if item.strip()
        ]

        extensions = [
            item.strip().lower()
            for item in section.get("extensions", "").splitlines()
            if item.strip()
        ]

        return cls(
            enabled=section.getboolean("enabled", fallback=True),
            root_dir=section.get("root_dir", fallback="."),
            include_dirs=include_dirs,
            extensions=extensions,
            batch_limit_kb=section.getint("batch_limit_kb", fallback=20),
            max_files=section.getint("max_files", fallback=100),
            encoding=section.get("encoding", fallback="utf-8"),
            include_manifest=section.getboolean(
                "include_manifest",
                fallback=True,
            ),
            include_file_metadata=section.getboolean(
                "include_file_metadata",
                fallback=True,
            ),
            sort_files=section.getboolean(
                "sort_files",
                fallback=True,
            ),
        )

    @property
    def batch_limit_bytes(self) -> int:
        return self.batch_limit_kb * 1024


class ContextBuilder:
    """
    将指定目录中的项目文件转换为纯文本，并按照批次组织。

    特点：
    - 只扫描 include_dirs 中明确指定的目录。
    - 不扫描 prompt_set。
    - 不修改原文件。
    - 不负责浏览器操作。
    - 不负责发送消息。
    - 一个文件绝不会被拆到多个 batch。
    - 普通情况下每个 batch 的最终文本 <= batch_limit。
    - 如果单个文件自身超过 batch_limit，则该文件独立成为一个 batch。
    """

    def __init__(self, config: ContextConfig):
        self.config = config

    def build(self) -> ContextResult:
        if not self.config.enabled:
            return ContextResult(
                success=True,
                batches=[],
                files=[],
                skipped_files=[],
                total_bytes=0,
                total_context_bytes=0,
                total_characters=0,
            )

        try:
            root = Path(self.config.root_dir).resolve()

            if not root.exists():
                raise FileNotFoundError(
                    f"Context root directory does not exist: {root}"
                )

            if not root.is_dir():
                raise NotADirectoryError(
                    f"Context root path is not a directory: {root}"
                )

            discovered_paths, skipped_files = self._discover_files(root)

            if self.config.sort_files:
                discovered_paths.sort(
                    key=lambda path: path.relative_to(root).as_posix().lower()
                )

            files: list[ContextFile] = []

            for path in discovered_paths:
                if len(files) >= self.config.max_files:
                    skipped_files.append(
                        f"{self._relative_path(path, root)} "
                        f"(max_files={self.config.max_files})"
                    )
                    break

                context_file = self._read_file(path, root)

                if context_file is None:
                    skipped_files.append(
                        self._relative_path(path, root)
                    )
                    continue

                files.append(context_file)

            batches = self._build_batches(files)

            total_bytes = sum(
                item.size_bytes
                for item in files
            )

            total_context_bytes = sum(
                batch.size_bytes
                for batch in batches
            )

            total_characters = sum(
                len(batch.text)
                for batch in batches
            )

            return ContextResult(
                success=True,
                batches=batches,
                files=files,
                skipped_files=skipped_files,
                total_bytes=total_bytes,
                total_context_bytes=total_context_bytes,
                total_characters=total_characters,
            )

        except Exception as exc:
            return ContextResult(
                success=False,
                batches=[],
                files=[],
                skipped_files=[],
                total_bytes=0,
                total_context_bytes=0,
                total_characters=0,
                error=str(exc),
            )

    def _discover_files(
        self,
        root: Path,
    ) -> tuple[list[Path], list[str]]:
        discovered: list[Path] = []
        skipped: list[str] = []

        include_dirs = self.config.include_dirs or []

        for include_dir in include_dirs:
            candidate = (root / include_dir).resolve()

            if not self._is_inside_root(candidate, root):
                skipped.append(
                    f"{include_dir} (outside root_dir)"
                )
                continue

            if not candidate.exists():
                skipped.append(
                    f"{include_dir} (directory does not exist)"
                )
                continue

            if not candidate.is_dir():
                skipped.append(
                    f"{include_dir} (not a directory)"
                )
                continue

            for path in candidate.rglob("*"):
                if not path.is_file():
                    continue

                resolved_path = path.resolve()

                if not self._is_inside_root(resolved_path, root):
                    skipped.append(
                        f"{self._relative_path(path, root)} "
                        f"(outside root_dir)"
                    )
                    continue

                if not self._extension_allowed(resolved_path):
                    continue

                discovered.append(resolved_path)

        # 防止多个 include_dirs 重叠时重复发送同一个文件。
        unique_paths: dict[Path, Path] = {}

        for path in discovered:
            unique_paths[path] = path

        return list(unique_paths.values()), skipped

    def _read_file(
        self,
        path: Path,
        root: Path,
    ) -> ContextFile | None:
        try:
            content = path.read_text(
                encoding=self.config.encoding
            )

            return ContextFile(
                path=self._relative_path(path, root),
                absolute_path=path,
                size_bytes=path.stat().st_size,
                content=content,
            )

        except (OSError, UnicodeError):
            return None

    def _build_batches(
        self,
        files: list[ContextFile],
    ) -> list[ContextBatch]:
        batches: list[ContextBatch] = []

        current_files: list[ContextFile] = []
        current_sections: list[str] = []
        current_size = 0

        for context_file in files:
            section = self._render_file(context_file)
            section_size = self._text_size_bytes(section)

            if not current_files:
                current_files.append(context_file)
                current_sections.append(section)
                current_size = section_size

                # 即使这个文件超过 20 KB，也不能拆分。
                if section_size > self.config.batch_limit_bytes:
                    batches.append(
                        self._make_batch(
                            len(batches) + 1,
                            current_files,
                            current_sections,
                        )
                    )

                    current_files = []
                    current_sections = []
                    current_size = 0

                continue

            if (
                current_size + section_size
                <= self.config.batch_limit_bytes
            ):
                current_files.append(context_file)
                current_sections.append(section)
                current_size += section_size
                continue

            # 当前 batch 已经不能再加入这个完整文件。
            batches.append(
                self._make_batch(
                    len(batches) + 1,
                    current_files,
                    current_sections,
                )
            )

            current_files = [context_file]
            current_sections = [section]
            current_size = section_size

            # 当前文件自身超过限制，立即独立成 batch。
            if section_size > self.config.batch_limit_bytes:
                batches.append(
                    self._make_batch(
                        len(batches) + 1,
                        current_files,
                        current_sections,
                    )
                )

                current_files = []
                current_sections = []
                current_size = 0

        if current_files:
            batches.append(
                self._make_batch(
                    len(batches) + 1,
                    current_files,
                    current_sections,
                )
            )

        return batches

    def _make_batch(
        self,
        index: int,
        files: list[ContextFile],
        sections: list[str],
    ) -> ContextBatch:
        parts: list[str] = []

        if self.config.include_manifest:
            parts.append(
                self._render_manifest(files)
            )

        parts.extend(sections)

        text = "\n\n".join(parts)

        return ContextBatch(
            index=index,
            files=tuple(files),
            text=text,
            size_bytes=self._text_size_bytes(text),
        )

    def _render_manifest(
        self,
        files: list[ContextFile],
    ) -> str:
        lines = [
            "[CONTEXT_MANIFEST]",
            f"FILES_IN_BATCH: {len(files)}",
        ]

        for context_file in files:
            lines.append(
                f"- {context_file.path}"
            )

        lines.append("[/CONTEXT_MANIFEST]")

        return "\n".join(lines)

    def _render_file(
        self,
        context_file: ContextFile,
    ) -> str:
        lines = [
            "[FILE]",
            f"PATH: {context_file.path}",
        ]

        if self.config.include_file_metadata:
            lines.append(
                f"SIZE_BYTES: {context_file.size_bytes}"
            )

        lines.extend(
            [
                "",
                context_file.content,
                "",
                "[/FILE]",
            ]
        )

        return "\n".join(lines)

    def _extension_allowed(
        self,
        path: Path,
    ) -> bool:
        extensions = self.config.extensions or []

        if not extensions:
            return True

        return path.suffix.lower() in extensions

    @staticmethod
    def _text_size_bytes(text: str) -> int:
        return len(text.encode("utf-8"))

    @staticmethod
    def _is_inside_root(
        path: Path,
        root: Path,
    ) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _relative_path(
        path: Path,
        root: Path,
    ) -> str:
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return str(path)


def build_context_from_file(
    config_path: str | Path = "context_config.txt",
) -> ContextResult:
    """
    使用配置文件创建 ContextBuilder 并生成 context batches。
    """
    config = ContextConfig.from_file(config_path)
    builder = ContextBuilder(config)
    return builder.build()


def main() -> int:
    config_path = Path(__file__).with_name(
        "context_config.txt"
    )

    result = build_context_from_file(config_path)

    if not result.success:
        print(f"[CONTEXT] ERROR: {result.error}")
        return 1

    print(
        f"[CONTEXT] files={len(result.files)} "
        f"batches={len(result.batches)} "
        f"raw_bytes={result.total_bytes} "
        f"context_bytes={result.total_context_bytes}"
    )

    for batch in result.batches:
        print(
            f"[CONTEXT] batch={batch.index} "
            f"files={len(batch.files)} "
            f"bytes={batch.size_bytes}"
        )

        for context_file in batch.files:
            print(
                f"  - {context_file.path} "
                f"({context_file.size_bytes} bytes)"
            )

    if result.skipped_files:
        print("[CONTEXT] skipped:")
        for item in result.skipped_files:
            print(f"  - {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
