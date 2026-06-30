from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from .scrapers.baw import BawScraper
from .scrapers.centauro import CentauroScraper
from .scrapers.netshoes import (
    NetshoesAdidasOriginalsScraper,
    NetshoesAdidasScraper,
    NetshoesBawScraper,
    NetshoesScraper,
)
from .scrapers.approve import ApproveScraper
from .scrapers.ous import OusScraper
from .scrapers.umbro import UmbroScraper


@dataclass(frozen=True)
class SourceConfig:
    key: str
    label: str
    emoji: str
    scraper_factory: Callable
    color: str
    bg: str
    border: str
    run_in_ci: bool = True
    needs_playwright: bool = False


SOURCES: Dict[str, SourceConfig] = {
    "ous": SourceConfig(
        key="ous",
        label="OUS oficial",
        emoji="🟧",
        scraper_factory=OusScraper,
        color="#ff7a00",
        bg="rgba(255, 122, 0, 0.15)",
        border="#ff7a00",
    ),
    "netshoes": SourceConfig(
        key="netshoes",
        label="Netshoes OUS",
        emoji="🟦",
        scraper_factory=NetshoesScraper,
        color="#70a1ff",
        bg="rgba(112, 161, 255, 0.15)",
        border="#70a1ff",
    ),
    "centauro": SourceConfig(
        key="centauro",
        label="Centauro OUS",
        emoji="🟥",
        scraper_factory=CentauroScraper,
        color="#ff4757",
        bg="rgba(255, 71, 87, 0.15)",
        border="#ff4757",
        run_in_ci=False,
        needs_playwright=True,
    ),
    "baw": SourceConfig(
        key="baw",
        label="BaW Clothing",
        emoji="⚫",
        scraper_factory=BawScraper,
        color="#ffffff",
        bg="rgba(255, 255, 255, 0.1)",
        border="#555555",
    ),
    "netshoes_baw": SourceConfig(
        key="netshoes_baw",
        label="Netshoes BaW",
        emoji="🟪",
        scraper_factory=NetshoesBawScraper,
        color="#5352ed",
        bg="rgba(83, 82, 237, 0.15)",
        border="#5352ed",
    ),
    "netshoes_adidas": SourceConfig(
        key="netshoes_adidas",
        label="Netshoes Adidas",
        emoji="🟢",
        scraper_factory=NetshoesAdidasScraper,
        color="#2ed573",
        bg="rgba(46, 213, 115, 0.15)",
        border="#2ed573",
    ),
    "netshoes_adidas_originals": SourceConfig(
        key="netshoes_adidas_originals",
        label="Netshoes Adidas Originals",
        emoji="🔵",
        scraper_factory=NetshoesAdidasOriginalsScraper,
        color="#1e90ff",
        bg="rgba(30, 144, 255, 0.15)",
        border="#1e90ff",
    ),
    "umbro": SourceConfig(
        key="umbro",
        label="Umbro Oficial",
        emoji="⚽",
        scraper_factory=UmbroScraper,
        color="#00a3e0",
        bg="rgba(0, 163, 224, 0.15)",
        border="#00a3e0",
    ),
    "approve": SourceConfig(
        key="approve",
        label="Approve",
        emoji="🔴",
        scraper_factory=ApproveScraper,
        color="#ff3838",
        bg="rgba(255, 56, 56, 0.15)",
        border="#ff3838",
    ),
}


def source_keys() -> List[str]:
    return list(SOURCES)


def ci_source_keys() -> List[str]:
    return [key for key, cfg in SOURCES.items() if cfg.run_in_ci]


def source_labels() -> Dict[str, str]:
    return {key: cfg.label for key, cfg in SOURCES.items()}


def source_emojis() -> Dict[str, str]:
    return {key: cfg.emoji for key, cfg in SOURCES.items()}


def dashboard_source_config() -> Dict[str, dict]:
    return {
        key: {
            "label": cfg.label,
            "color": cfg.color,
            "bg": cfg.bg,
            "border": cfg.border,
        }
        for key, cfg in SOURCES.items()
    }
