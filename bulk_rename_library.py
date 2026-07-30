from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from pikaraoke.lib.karaoke_database import KaraokeDatabase
from pikaraoke.lib.metadata_parser import (
    get_song_correct_name,
    has_artist_title_separator,
    regex_tidy,
    youtube_id_suffix,
)
from pikaraoke.lib.song_list import SongList
from pikaraoke.lib.song_manager import SongManager, sanitize_filename


@dataclass
class RenameDecision:
    source: Path
    target_name: str | None
    status: str
    detail: str = ""


def _iter_song_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SongList.VALID_EXTENSIONS:
            files.append(path)
    files.sort()
    return files


def _suggest_name(path: Path, use_lastfm: bool = False) -> str | None:
    stem = path.stem
    suffix = youtube_id_suffix(path.name)
    if suffix:
        stem = stem[: -len(suffix)]

    corrected: str | None = None
    tidied = regex_tidy(stem)
    if has_artist_title_separator(tidied):
        left, right = tidied.split(" - ", 1)
        corrected = f"{right.strip()} - {left.strip()}"
    elif use_lastfm:
        corrected = get_song_correct_name(
            stem,
            raw_filename=str(path),
            force_title_first=True,
        )
    if not corrected:
        return None

    corrected = sanitize_filename(corrected)
    return f"{corrected}{suffix}" if suffix else corrected


def _build_decisions(root: Path, use_lastfm: bool = False) -> list[RenameDecision]:
    decisions: list[RenameDecision] = []
    seen_targets: set[Path] = set()

    for song_path in _iter_song_files(root):
        target_name = _suggest_name(song_path, use_lastfm=use_lastfm)
        if not target_name:
            decisions.append(RenameDecision(song_path, None, "skip", "sem sugestao"))
            continue

        current_name = song_path.stem
        if current_name == target_name:
            decisions.append(RenameDecision(song_path, target_name, "keep", "ja esta padrao"))
            continue

        target_path = song_path.with_name(target_name + song_path.suffix)
        if target_path.exists() and target_path != song_path:
            decisions.append(
                RenameDecision(song_path, target_name, "conflict", "arquivo destino ja existe")
            )
            continue

        if target_path in seen_targets:
            decisions.append(
                RenameDecision(song_path, target_name, "conflict", "destino duplicado no lote")
            )
            continue

        seen_targets.add(target_path)
        decisions.append(RenameDecision(song_path, target_name, "rename"))

    return decisions


def _write_report(report_path: Path, decisions: list[RenameDecision]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as report:
        for item in decisions:
            target_display = item.target_name or "-"
            report.write(f"[{item.status}] {item.source.name} -> {target_display}")
            if item.detail:
                report.write(f" ({item.detail})")
            report.write("\n")


def _apply(root: Path, decisions: list[RenameDecision]) -> tuple[int, int, int]:
    db = KaraokeDatabase()
    manager = SongManager(str(root), db=db)

    renamed = 0
    kept = 0
    skipped = 0

    for item in decisions:
        if item.status == "rename" and item.target_name:
            try:
                manager.rename(str(item.source), item.target_name)
                renamed += 1
            except OSError:
                skipped += 1
        elif item.status == "keep":
            kept += 1
        else:
            skipped += 1

    return renamed, kept, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Renomeia a biblioteca inteira para o formato 'Musica - Artista'."
    )
    parser.add_argument("library_path", help="Caminho da biblioteca de musicas")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica as renomeacoes. Sem isso, executa apenas dry-run.",
    )
    parser.add_argument(
        "--report",
        default="bulk_rename_report.txt",
        help="Arquivo de relatorio gerado no final",
    )
    parser.add_argument(
        "--use-lastfm",
        action="store_true",
        help="Usa Last.fm para tentar sugerir mais nomes. Mais lento para bibliotecas grandes.",
    )
    args = parser.parse_args()

    root = Path(args.library_path).resolve()
    if not root.exists():
        print(f"Biblioteca nao encontrada: {root}")
        return 1

    decisions = _build_decisions(root, use_lastfm=args.use_lastfm)
    report_path = Path(args.report).resolve()
    _write_report(report_path, decisions)

    rename_count = sum(1 for item in decisions if item.status == "rename")
    keep_count = sum(1 for item in decisions if item.status == "keep")
    conflict_count = sum(1 for item in decisions if item.status == "conflict")
    skip_count = sum(1 for item in decisions if item.status == "skip")

    print(f"Biblioteca: {root}")
    print(f"Relatorio: {report_path}")
    print(f"Renomear: {rename_count}")
    print(f"Ja OK: {keep_count}")
    print(f"Conflitos: {conflict_count}")
    print(f"Sem sugestao: {skip_count}")

    if not args.apply:
        print("Dry-run concluido. Rode novamente com --apply para renomear de verdade.")
        return 0

    renamed, kept, skipped = _apply(root, decisions)
    print(f"Aplicado. Renomeadas: {renamed}, mantidas: {kept}, ignoradas: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
