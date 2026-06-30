# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List

from .sources import dashboard_source_config
from .storage import latest_source_runs

HTML_TEMPLATE = """<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Streetwear Price Monitor — Dashboard</title>
  
  <!-- Fontes Premium -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  
  <!-- Chart.js para Histórico de Preços -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

  <style>
    :root {
      --bg-main: hsl(224, 25%, 10%);
      --bg-card: hsl(224, 25%, 14%);
      --bg-control: hsl(224, 25%, 12%);
      --border-color: hsl(224, 20%, 20%);
      --border-hover: hsl(224, 20%, 30%);
      --text-main: hsl(210, 40%, 98%);
      --text-muted: hsl(215, 20%, 65%);
      --text-dim: hsl(215, 12%, 45%);
      
      --accent-ous: linear-gradient(135deg, #ff7a00, #ff4500);
      --accent-baw: linear-gradient(135deg, #333333, #111111);
      --accent-netshoes: linear-gradient(135deg, #0066cc, #0044aa);
      --accent-centauro: linear-gradient(135deg, #e50914, #990000);
      
      --primary: hsl(210, 100%, 55%);
      --primary-glow: rgba(0, 102, 204, 0.35);
      
      --discount-glow: rgba(229, 9, 20, 0.4);
      --discount-bg: #d8201a;
      
      --transition-base: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg-main);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
    }

    /* Scrollbar premium */
    ::-webkit-scrollbar {
      width: 8px;
      height: 8px;
    }
    ::-webkit-scrollbar-track {
      background: var(--bg-main);
    }
    ::-webkit-scrollbar-thumb {
      background: var(--border-color);
      border-radius: 4px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: var(--border-hover);
    }

    header {
      background: rgba(18, 22, 33, 0.85);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border-color);
      position: sticky;
      top: 0;
      z-index: 100;
      padding: 16px 24px;
    }

    .header-container {
      max-width: 1400px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .header-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }

    .logo-area {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .logo-area h1 {
      font-family: 'Outfit', sans-serif;
      font-size: 24px;
      font-weight: 800;
      background: linear-gradient(135deg, #fff 30%, var(--text-muted) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: -0.5px;
    }

    .live-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(46, 213, 115, 0.1);
      border: 1px solid rgba(46, 213, 115, 0.2);
      color: #2ed573;
      padding: 4px 10px;
      border-radius: 99px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .live-badge::before {
      content: '';
      width: 6px;
      height: 6px;
      background: #2ed573;
      border-radius: 50%;
      box-shadow: 0 0 8px #2ed573;
      animation: pulse 1.8s infinite;
    }

    @keyframes pulse {
      0% { transform: scale(0.9); opacity: 0.6; }
      50% { transform: scale(1.2); opacity: 1; box-shadow: 0 0 12px #2ed573; }
      100% { transform: scale(0.9); opacity: 0.6; }
    }

    .meta-info {
      font-size: 13px;
      color: var(--text-muted);
      text-align: right;
    }

    .meta-info span {
      font-weight: 500;
      color: var(--text-main);
    }

    /* Grid de Métricas */
    .metrics-row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-top: 4px;
    }

    .metric-card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 14px 18px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      transition: var(--transition-base);
    }

    .metric-card:hover {
      border-color: var(--border-hover);
      transform: translateY(-2px);
    }

    .metric-card .label {
      font-size: 12px;
      color: var(--text-muted);
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .metric-card .val {
      font-family: 'Outfit', sans-serif;
      font-size: 26px;
      font-weight: 700;
      color: #fff;
    }

    .metric-card .desc {
      font-size: 11px;
      color: var(--text-dim);
    }

    .metric-card.highlight {
      position: relative;
      overflow: hidden;
    }

    .metric-card.highlight::after {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: radial-gradient(circle, rgba(229, 9, 20, 0.08) 0%, transparent 70%);
      pointer-events: none;
    }

    .metric-card.highlight .val {
      color: #ff4757;
      text-shadow: 0 0 10px rgba(255, 71, 87, 0.25);
    }

    /* Barra de Controles / Filtros */
    .controls-wrapper {
      max-width: 1400px;
      margin: 20px auto 0;
      padding: 0 24px;
      width: 100%;
    }

    .controls-panel {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .search-row {
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }

    .search-input-wrapper {
      flex: 1;
      min-width: 280px;
      position: relative;
    }

    .search-input-wrapper input {
      width: 100%;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      padding: 12px 16px 12px 42px;
      color: #fff;
      font-size: 14px;
      font-family: inherit;
      transition: var(--transition-base);
    }

    .search-input-wrapper input:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px var(--primary-glow);
    }

    .search-input-wrapper svg {
      position: absolute;
      left: 14px;
      top: 50%;
      transform: translateY(-50%);
      width: 18px;
      height: 18px;
      color: var(--text-dim);
      pointer-events: none;
    }

    /* Switch de Promo */
    .promo-switch {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      cursor: pointer;
      user-select: none;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      padding: 8px 16px;
      border-radius: 10px;
      transition: var(--transition-base);
    }

    .promo-switch:hover {
      border-color: var(--border-hover);
    }

    .promo-switch input {
      display: none;
    }

    .switch-track {
      width: 38px;
      height: 20px;
      background: hsl(224, 20%, 25%);
      border-radius: 99px;
      position: relative;
      transition: var(--transition-base);
    }

    .switch-thumb {
      width: 14px;
      height: 14px;
      background: #fff;
      border-radius: 50%;
      position: absolute;
      top: 3px;
      left: 3px;
      transition: var(--transition-base);
    }

    .promo-switch input:checked + .switch-track {
      background: #ff4757;
      box-shadow: 0 0 10px rgba(255, 71, 87, 0.3);
    }

    .promo-switch input:checked + .switch-track .switch-thumb {
      left: 21px;
    }

    .switch-label {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
    }
    
    .promo-switch input:checked ~ .switch-label {
      color: #ff4757;
    }

    /* Grid/List layout toggle */
    .layout-toggle {
      display: flex;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      padding: 3px;
    }

    .layout-btn {
      background: transparent;
      border: none;
      color: var(--text-dim);
      padding: 8px 12px;
      border-radius: 7px;
      cursor: pointer;
      display: flex;
      align-items: center;
      transition: var(--transition-base);
    }

    .layout-btn:hover {
      color: var(--text-main);
    }

    .layout-btn.active {
      background: var(--bg-card);
      color: var(--primary);
      box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    }

    /* Linha de pílulas de filtros */
    .filter-group-title {
      font-size: 11px;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-weight: 600;
      margin-bottom: 8px;
    }

    .filters-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }
    
    @media (min-width: 900px) {
      .filters-grid {
        grid-template-columns: 2fr 1fr;
      }
    }

    .pill-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .pill-checkbox {
      display: none;
    }

    .filter-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 6px 14px;
      border-radius: 99px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      user-select: none;
      transition: var(--transition-base);
    }

    .filter-pill:hover {
      border-color: var(--border-hover);
      color: #fff;
    }

    .pill-checkbox:checked + .filter-pill {
      background: var(--primary);
      border-color: var(--primary);
      color: #fff;
      box-shadow: 0 0 10px rgba(0, 102, 204, 0.25);
    }

    /* Cores das fontes específicas nos botões */
    .filter-pill .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
    }

    /* Filtro de Tamanhos Avançado e Compacto */
    .sizes-scroll-container {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .size-filter-compact-row {
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }

    .fav-sizes-buttons {
      display: flex;
      gap: 8px;
    }

    .size-btn {
      min-width: 36px;
      height: 36px;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 600;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      cursor: pointer;
      user-select: none;
      transition: var(--transition-base);
    }

    .size-btn:hover {
      border-color: var(--border-hover);
      color: #fff;
    }

    .size-btn.active {
      background: var(--primary);
      border-color: var(--primary);
      color: #fff;
      box-shadow: 0 0 8px rgba(0, 102, 204, 0.3);
    }

    /* Destaque nos favoritos do Thierry (42 e 43) */
    .size-btn.fav {
      border-color: #ff9f43;
      color: #ff9f43;
      position: relative;
    }
    
    .size-btn.fav::after {
      content: '★';
      font-size: 8px;
      position: absolute;
      top: 1px;
      right: 2px;
    }
    
    .size-btn.fav.active {
      background: linear-gradient(135deg, #ff9f43, #ff793f);
      border-color: #ff793f;
      color: #fff;
      box-shadow: 0 0 8px rgba(255, 159, 67, 0.4);
    }

    .toggle-sizes-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 8px 14px;
      font-size: 12px;
      font-weight: 600;
      border-radius: 8px;
      cursor: pointer;
      transition: var(--transition-base);
    }

    .toggle-sizes-btn:hover {
      border-color: var(--border-hover);
      color: #fff;
    }

    .toggle-sizes-btn.panel-open {
      background: rgba(255, 255, 255, 0.05);
      border-color: var(--border-hover);
      color: #fff;
    }

    .caret-icon {
      transition: transform 0.3s ease;
    }

    .toggle-sizes-btn.panel-open .caret-icon {
      transform: rotate(180deg);
    }

    #all-sizes-panel {
      max-height: 0;
      opacity: 0;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      gap: 12px;
      transition: all 0.35s cubic-bezier(0.4, 0, 0.2, 1);
    }

    #all-sizes-panel.open {
      max-height: 240px;
      opacity: 1;
      background: rgba(18, 22, 33, 0.4);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 14px;
      margin-top: 4px;
    }

    .sizes-panel-tabs {
      display: flex;
      gap: 6px;
      border-bottom: 1px solid var(--border-color);
      padding-bottom: 8px;
    }

    .size-tab-btn {
      background: transparent;
      border: none;
      color: var(--text-dim);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      transition: var(--transition-base);
    }

    .size-tab-btn:hover {
      color: var(--text-main);
    }

    .size-tab-btn.active {
      background: var(--bg-control);
      color: var(--primary);
      box-shadow: inset 0 0 0 1px var(--border-color);
    }

    .sizes-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      overflow-y: auto;
      max-height: 120px;
      padding-right: 4px;
    }

    /* Estilização da scrollbar da grade de tamanhos */
    .sizes-grid::-webkit-scrollbar {
      width: 4px;
    }
    .sizes-grid::-webkit-scrollbar-track {
      background: transparent;
    }
    .sizes-grid::-webkit-scrollbar-thumb {
      background: var(--border-color);
      border-radius: 2px;
    }

    /* Filtro de Desconto Mínimo */
    .discount-filters {
      display: flex;
      gap: 8px;
    }

    .disc-btn {
      flex: 1;
      padding: 8px 12px;
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 600;
      border-radius: 8px;
      cursor: pointer;
      transition: var(--transition-base);
      text-align: center;
    }

    .disc-btn:hover {
      border-color: var(--border-hover);
      color: #fff;
    }

    .disc-btn.active {
      background: rgba(255, 71, 87, 0.15);
      border-color: #ff4757;
      color: #ff4757;
      box-shadow: 0 0 8px rgba(255, 71, 87, 0.15);
    }

    /* Conteúdo Principal */
    main {
      flex: 1;
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
      width: 100%;
    }

    .grid-view {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 20px;
    }

    /* Card Premium de Produto */
    .product-card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      height: 100%;
      position: relative;
      transition: var(--transition-base);
      cursor: pointer;
    }

    .product-card:hover {
      transform: translateY(-5px);
      border-color: var(--border-hover);
      box-shadow: 0 10px 20px rgba(0, 0, 0, 0.3), 
                  0 0 1px 1px rgba(255, 255, 255, 0.05);
    }

    /* Efeito de neon pra descontos insanos */
    .product-card.insane-promo:hover {
      box-shadow: 0 10px 25px rgba(255, 71, 87, 0.15), 
                  0 0 0 1px rgba(255, 71, 87, 0.2);
    }

    .card-image-wrapper {
      width: 100%;
      aspect-ratio: 1 / 1;
      background: #181d28;
      overflow: hidden;
      position: relative;
    }

    .card-image-wrapper img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      transition: transform 0.4s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .product-card:hover .card-image-wrapper img {
      transform: scale(1.06);
    }

    .card-badges {
      position: absolute;
      top: 12px;
      left: 12px;
      right: 12px;
      display: flex;
      justify-content: space-between;
      pointer-events: none;
      z-index: 2;
    }

    .discount-badge {
      background: #ff4757;
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 8px;
      box-shadow: 0 4px 10px rgba(255, 71, 87, 0.4);
    }

    .store-badge {
      font-size: 10px;
      font-weight: 700;
      color: #fff;
      padding: 4px 10px;
      border-radius: 8px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      box-shadow: 0 4px 10px rgba(0, 0, 0, 0.2);
    }

    .card-body {
      padding: 16px;
      display: flex;
      flex-direction: column;
      flex: 1;
      gap: 10px;
    }

    .product-brand {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .product-title {
      font-size: 14px;
      font-weight: 600;
      color: #fff;
      line-height: 1.4;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      height: 40px;
    }

    .card-prices {
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin-top: auto;
    }

    .price-now {
      font-family: 'Outfit', sans-serif;
      font-size: 18px;
      font-weight: 700;
      color: #fff;
    }

    .price-old {
      font-family: 'Outfit', sans-serif;
      font-size: 13px;
      text-decoration: line-through;
      color: var(--text-dim);
    }

    .card-sizes {
      border-top: 1px solid var(--border-color);
      padding-top: 10px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .sizes-title {
      font-size: 10px;
      color: var(--text-dim);
      text-transform: uppercase;
      font-weight: 600;
    }

    .sizes-list {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      height: 20px;
      overflow: hidden;
    }

    .size-tag {
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      font-size: 9px;
      font-weight: 600;
      padding: 1px 4px;
      border-radius: 4px;
    }

    .size-tag.fav {
      border-color: rgba(255, 159, 67, 0.4);
      color: #ff9f43;
      font-weight: 700;
    }

    /* List View (Tabela Premium) */
    .list-view-container {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      overflow: hidden;
      width: 100%;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13px;
    }

    th, td {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-color);
    }

    th {
      background: rgba(18, 22, 33, 0.4);
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: 0.5px;
      cursor: pointer;
      user-select: none;
      transition: var(--transition-base);
    }

    th:hover {
      color: #fff;
      background: rgba(18, 22, 33, 0.6);
    }

    th.sorted-asc::after {
      content: ' ▲';
      color: var(--primary);
    }

    th.sorted-desc::after {
      content: ' ▼';
      color: var(--primary);
    }

    tr {
      transition: var(--transition-base);
      cursor: pointer;
    }

    tr:hover {
      background: rgba(255, 255, 255, 0.02);
    }

    .td-img {
      width: 44px;
      height: 44px;
      border-radius: 8px;
      object-fit: cover;
      background: #181d28;
    }

    .td-name {
      font-weight: 600;
      color: #fff;
    }

    .td-price-now {
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      color: #fff;
    }

    .td-price-old {
      font-family: 'Outfit', sans-serif;
      text-decoration: line-through;
      color: var(--text-dim);
    }

    .td-pct {
      color: #ff4757;
      font-weight: 700;
    }

    /* Modal / Drawer Premium de Detalhes */
    .modal-overlay {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(8px);
      z-index: 1000;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s ease;
      padding: 16px;
    }

    .modal-overlay.open {
      opacity: 1;
      pointer-events: auto;
    }

    .modal-container {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      width: 100%;
      max-width: 860px;
      border-radius: 20px;
      overflow: hidden;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
      transform: scale(0.95) translateY(20px);
      transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
      display: flex;
      flex-direction: column;
      max-height: 90vh;
    }

    .modal-overlay.open .modal-container {
      transform: scale(1) translateY(0);
    }

    .modal-header {
      padding: 20px 24px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    .modal-close {
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      width: 36px;
      height: 36px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: var(--transition-base);
    }

    .modal-close:hover {
      background: #ff4757;
      border-color: #ff4757;
      color: #fff;
    }

    .modal-scrollable {
      overflow-y: auto;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    .product-detail-hero {
      display: grid;
      grid-template-columns: 1fr;
      gap: 24px;
    }

    @media (min-width: 600px) {
      .product-detail-hero {
        grid-template-columns: 240px 1fr;
      }
    }

    .detail-img {
      width: 100%;
      aspect-ratio: 1/1;
      border-radius: 12px;
      object-fit: cover;
      background: #181d28;
      border: 1px solid var(--border-color);
    }

    .detail-info {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .detail-info h2 {
      font-size: 20px;
      font-weight: 700;
      color: #fff;
      line-height: 1.3;
    }

    .detail-prices-row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .detail-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      background: var(--primary);
      color: #fff;
      padding: 12px 24px;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 600;
      text-decoration: none;
      transition: var(--transition-base);
      margin-top: auto;
      border: none;
      cursor: pointer;
      box-shadow: 0 4px 12px rgba(0, 102, 204, 0.3);
    }

    .detail-btn:hover {
      background: hsl(210, 100%, 60%);
      box-shadow: 0 4px 16px rgba(0, 102, 204, 0.4);
    }

    /* Secção de Gráfico */
    .chart-section {
      background: rgba(18, 22, 33, 0.4);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 18px;
    }

    .chart-title {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .chart-container {
      position: relative;
      width: 100%;
      height: 220px;
    }

    /* Estado Vazio */
    .empty-state {
      grid-column: 1 / -1;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 48px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 14px;
      text-align: center;
    }

    .empty-state svg {
      width: 48px;
      height: 48px;
      color: var(--text-dim);
    }

    .empty-state p {
      font-size: 15px;
      color: var(--text-muted);
    }

    .reset-btn {
      background: var(--bg-control);
      border: 1px solid var(--border-color);
      color: #fff;
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: var(--transition-base);
    }

    .reset-btn:hover {
      border-color: var(--border-hover);
      background: var(--border-color);
    }
  </style>
</head>
<body>

  <header>
    <div class="header-container">
      <div class="header-top">
        <div class="logo-area">
          <h1>Streetwear Price Monitor</h1>
          <span class="live-badge">Live DB</span>
        </div>
        <div class="meta-info">
          Catálogo: <span id="meta-total">0</span> produtos · <span id="meta-promos" style="color:#ff4757">0</span> em promoção<br>
          Gerado: <span id="meta-date">--</span>
        </div>
      </div>
      
      <!-- Linha de Métricas Dinâmicas -->
      <div class="metrics-row">
        <div class="metric-card">
          <span class="label">Total Monitorados</span>
          <span class="val" id="stat-total">0</span>
          <span class="desc">Produtos ativos indexados</span>
        </div>
        <div class="metric-card highlight">
          <span class="label">Em Promoção</span>
          <span class="val" id="stat-promos">0</span>
          <span class="desc" id="stat-promos-pct">0% do catálogo total</span>
        </div>
        <div class="metric-card">
          <span class="label">Desconto Médio</span>
          <span class="val" id="stat-avg-disc">0%</span>
          <span class="desc">Média de desconto ativo</span>
        </div>
        <div class="metric-card">
          <span class="label">Maior Desconto</span>
          <span class="val" id="stat-max-disc">0%</span>
          <span class="desc" id="stat-max-desc">--</span>
        </div>
      </div>
    </div>
  </header>

  <!-- Barra de Filtros e Controles -->
  <div class="controls-wrapper">
    <div class="controls-panel">
      <!-- Busca e Modo -->
      <div class="search-row">
        <div class="search-input-wrapper">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
          <input id="search-input" type="search" placeholder="Buscar por modelo, SKU ou tag..." autofocus>
        </div>
        
        <label class="promo-switch">
          <input type="checkbox" id="promo-only-check">
          <div class="switch-track">
            <div class="switch-thumb"></div>
          </div>
          <span class="switch-label">Só em promoção</span>
        </label>
        
        <div class="layout-toggle">
          <button id="layout-grid" class="layout-btn active" title="Visualização em Grid">
            <svg width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
          </button>
          <button id="layout-list" class="layout-btn" title="Visualização em Lista">
            <svg width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"></path></svg>
          </button>
        </div>
      </div>

      <!-- Filtros Detalhados -->
      <div class="filters-grid">
        <!-- Fontes e Lojas -->
        <div>
          <div class="filter-group-title">Lojas e Fontes</div>
          <div class="pill-list" id="source-filters-list">
            <!-- Gerado via JS -->
          </div>
        </div>

        <!-- Faixas de Desconto -->
        <div>
          <div class="filter-group-title">Faixa de Desconto</div>
          <div class="discount-filters">
            <button class="disc-btn active" data-min="0">Todos</button>
            <button class="disc-btn" data-min="20">20%+ OFF</button>
            <button class="disc-btn" data-min="40">40%+ OFF</button>
            <button class="disc-btn" data-min="50">50%+ OFF</button>
          </div>
        </div>
      </div>

      <!-- Grade de Tamanhos Premium e Compacta -->
      <div class="sizes-scroll-container">
        <div class="filter-group-title">Filtro de Tamanho</div>
        <div class="size-filter-compact-row">
          <!-- Favoritos sempre visíveis -->
          <div class="fav-sizes-buttons" id="fav-sizes-container">
            <!-- Gerado via JS para destacar os favoritos da pessoa -->
          </div>
          
          <!-- Botão para abrir os outros -->
          <button id="toggle-all-sizes-btn" class="toggle-sizes-btn">
            Mais Tamanhos
            <svg class="caret-icon" width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path></svg>
          </button>
        </div>

        <!-- Painel expansível de outros tamanhos -->
        <div id="all-sizes-panel">
          <div class="sizes-panel-tabs">
            <button class="size-tab-btn active" data-tab="sneakers" id="tab-sneakers-btn">Calçados (34-46)</button>
            <button class="size-tab-btn" data-tab="clothing" id="tab-clothing-btn">Vestuário (P-GG / Único)</button>
          </div>
          <div class="sizes-grid" id="sizes-filters-grid">
            <!-- Outros tamanhos gerados dinamicamente via JS -->
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Workspace de Visualização -->
  <main>
    <div id="catalog-container">
      <!-- JS injetará grid-view ou list-view -->
    </div>
  </main>

  <!-- Modal Overlay -->
  <div class="modal-overlay" id="product-modal">
    <div class="modal-container">
      <div class="modal-header">
        <h3 id="modal-title-brand" style="font-size:12px; color:var(--text-dim); text-transform:uppercase; font-weight:700;">--</h3>
        <button class="modal-close" id="modal-close-btn">&times;</button>
      </div>
      <div class="modal-scrollable">
        <div class="product-detail-hero">
          <img src="" alt="" class="detail-img" id="modal-img">
          <div class="detail-info">
            <h2 id="modal-title">Nome do Produto</h2>
            <p id="modal-sku" style="font-size: 12px; color: var(--text-dim);">SKU: --</p>
            
            <div class="detail-prices-row">
              <span class="price-now" id="modal-price" style="font-size:24px;">R$ 0,00</span>
              <span class="price-old" id="modal-price-old" style="font-size:16px;">R$ 0,00</span>
              <span class="discount-badge" id="modal-discount" style="font-size:13px; font-weight:700;">-0%</span>
            </div>
            
            <div style="margin-top: 10px; display:flex; flex-direction:column; gap:6px;">
              <p style="font-size: 13px; color: var(--text-muted);">
                Disponibilidade: <span id="modal-available" style="font-weight:600; color:#fff;">--</span>
              </p>
              <p style="font-size: 13px; color: var(--text-muted);" id="modal-stock-row">
                Estoque aproximado: <span id="modal-stock" style="font-weight:600; color:#fff;">--</span>
              </p>
              <div style="font-size: 13px; color: var(--text-muted); display:flex; flex-direction:column; gap:4px; margin-top:4px;">
                <span>Tamanhos em estoque:</span>
                <div class="sizes-grid" id="modal-sizes-list"></div>
              </div>
            </div>

            <a href="" target="_blank" rel="noopener" class="detail-btn" id="modal-link">
              Ir para Loja Oficial
              <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
            </a>
          </div>
        </div>

        <!-- Seção de Histórico de Preços -->
        <div class="chart-section">
          <span class="chart-title">
            <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2h-2a2 2 0 00-2 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path></svg>
            Histórico de Oscilação de Preços (30 dias)
          </span>
          <div class="chart-container">
            <canvas id="priceChart"></canvas>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Banco de Dados Embarcado -->
  <script id="embedded-data" type="application/json">
    %%PRODUCTS_JSON%%
  </script>

  <!-- Orquestração / Engine do Dashboard -->
  <script>
    (() => {
      function safeDecode(str) {
        if (!str) return '';
        try {
          return decodeURIComponent(str);
        } catch (e) {
          try {
            return decodeURI(str);
          } catch (err) {
            return str;
          }
        }
      }

      // Carregar os dados
      const payload = JSON.parse(document.getElementById('embedded-data').textContent);
      const allProducts = payload.products;
      const metadata = payload.metadata;

      // Injetar informações globais
      document.getElementById('meta-total').textContent = allProducts.length;
      document.getElementById('meta-promos').textContent = allProducts.filter(p => p.price < p.list_price).length;
      document.getElementById('meta-date').textContent = new Date(metadata.generated_at).toLocaleString('pt-BR');

      // Estado local do Dashboard
      const state = {
        search: '',
        promoOnly: false,
        layout: 'grid', // 'grid' | 'list'
        activeSources: new Set(),
        activeSize: null,
        minDiscount: 0,
        sortBy: 'discount_desc' // 'discount_desc' | 'price_asc' | 'price_desc' | 'name_asc'
      };

      // Mapeamento e Configuração de Lojas
      const SOURCE_CONFIG = %%SOURCE_CONFIG_JSON%%;

      // Extração de fontes únicas e inicialização
      const rawSources = [...new Set(allProducts.map(p => p.source))];
      rawSources.forEach(s => state.activeSources.add(s));

      // Extração de tamanhos únicos
      const allSizesSet = new Set();
      allProducts.forEach(p => {
        if (p.sizes) {
          p.sizes.forEach(sz => {
            if (sz.trim()) allSizesSet.add(sz.trim());
          });
        }
      });
      // Ordenação inteligente de tamanhos (numéricos primeiro)
      const sortedSizes = [...allSizesSet].sort((a, b) => {
        const na = parseFloat(a), nb = parseFloat(b);
        if (isNaN(na) || isNaN(nb)) {
          return a.localeCompare(b);
        }
        return na - nb;
      });

      // Elementos do DOM
      const searchInput = document.getElementById('search-input');
      const promoOnlyCheck = document.getElementById('promo-only-check');
      const layoutGridBtn = document.getElementById('layout-grid');
      const layoutListBtn = document.getElementById('layout-list');
      const sourceFiltersList = document.getElementById('source-filters-list');
      const sizesFiltersGrid = document.getElementById('sizes-filters-grid');
      const catalogContainer = document.getElementById('catalog-container');
      const discButtons = document.querySelectorAll('.disc-btn');
      
      // Elementos adicionais do DOM para tamanhos compactos
      const favSizesContainer = document.getElementById('fav-sizes-container');
      const toggleAllSizesBtn = document.getElementById('toggle-all-sizes-btn');
      const allSizesPanel = document.getElementById('all-sizes-panel');
      const tabSneakersBtn = document.getElementById('tab-sneakers-btn');
      const tabClothingBtn = document.getElementById('tab-clothing-btn');

      // Estado local de tamanho
      state.activeSizeTab = 'sneakers';

      // Chart.js local instance
      let priceChartInstance = null;

      // Gerar botões de Lojas
      rawSources.forEach(src => {
        const cfg = SOURCE_CONFIG[src] || { label: src, color: '#fff', bg: '#222', border: '#444' };
        const count = allProducts.filter(p => p.source === src).length;
        const promoCount = allProducts.filter(p => p.source === src && p.price < p.list_price).length;
        
        const label = document.createElement('label');
        label.className = 'pill-label';
        label.innerHTML = `
          <input type="checkbox" class="pill-checkbox" checked data-src="${src}">
          <span class="filter-pill" style="border-color:${cfg.border}">
            <span class="dot" style="background:${cfg.color}"></span>
            ${cfg.label}
            <small style="color:var(--text-dim)">(${count} / ${promoCount} promo)</small>
          </span>
        `;
        sourceFiltersList.appendChild(label);
      });

      // Helper para classificar tamanhos
      function isNumericSize(size) {
        return /^\d+(\.\d+)?$/.test(size);
      }

      // Função para renderizar a lista de botões de tamanho
      function renderSizesFilters() {
        favSizesContainer.innerHTML = '';
        sizesFiltersGrid.innerHTML = '';

        // 1. Renderizar Favoritos (42 e 43)
        const favs = ['42', '43'];
        favs.forEach(sz => {
          if (sortedSizes.includes(sz)) {
            const btn = document.createElement('div');
            btn.className = `size-btn fav ${state.activeSize === sz ? 'active' : ''}`;
            btn.dataset.size = sz;
            btn.textContent = `⭐ ${sz}`;
            favSizesContainer.appendChild(btn);
          }
        });

        // 2. Renderizar os outros tamanhos na aba ativa
        sortedSizes.forEach(size => {
          if (size === '42' || size === '43') return;

          const isNumeric = isNumericSize(size);
          const shouldRender = (state.activeSizeTab === 'sneakers' && isNumeric) ||
                               (state.activeSizeTab === 'clothing' && !isNumeric);

          if (shouldRender) {
            const btn = document.createElement('div');
            btn.className = `size-btn ${state.activeSize === size ? 'active' : ''}`;
            btn.dataset.size = size;
            btn.textContent = size;
            sizesFiltersGrid.appendChild(btn);
          }
        });
      }

      // Inicializa a renderização dos filtros de tamanho
      renderSizesFilters();

      // Formatador de Moeda
      function fmtBRL(val) {
        if (val === null || val === undefined) return '—';
        return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(val);
      }

      // Filtragem Geral de Dados
      function getFilteredProducts() {
        return allProducts.filter(p => {
          // Busca textual
          const q = state.search.toLowerCase().trim();
          const nameDec = safeDecode(p.name).toLowerCase();
          const brandDec = safeDecode(p.brand).toLowerCase();
          const matchSearch = !q || nameDec.includes(q) || p.sku.toLowerCase().includes(q) || (p.brand && brandDec.includes(q));
          
          // Só promoção
          const matchPromo = !state.promoOnly || (p.list_price && p.price < p.list_price);
          
          // Loja ativa
          const matchSource = state.activeSources.has(p.source);
          
          // Tamanho disponível
          const matchSize = !state.activeSize || (p.sizes && p.sizes.includes(state.activeSize));
          
          // Desconto mínimo
          const discountPct = p.list_price ? Math.round((1 - p.price / p.list_price) * 100) : 0;
          const matchDiscount = discountPct >= state.minDiscount;

          return matchSearch && matchPromo && matchSource && matchSize && matchDiscount;
        }).sort((a, b) => {
          const discA = a.list_price ? (1 - a.price / a.list_price) : 0;
          const discB = b.list_price ? (1 - b.price / b.list_price) : 0;
          
          if (state.sortBy === 'discount_desc') {
            return discB - discA;
          } else if (state.sortBy === 'price_asc') {
            return a.price - b.price;
          } else if (state.sortBy === 'price_desc') {
            return b.price - a.price;
          } else if (state.sortBy === 'name_asc') {
            return a.name.localeCompare(b.name);
          }
          return 0;
        });
      }

      // Renderizar Métricas de Destaque Dinâmicas
      function updateDynamicMetrics(filteredList) {
        const total = filteredList.length;
        const promos = filteredList.filter(p => p.list_price && p.price < p.list_price);
        const promosCount = promos.length;
        const promosPct = total > 0 ? Math.round((promosCount / total) * 100) : 0;

        document.getElementById('stat-total').textContent = total;
        document.getElementById('stat-promos').textContent = promosCount;
        document.getElementById('stat-promos-pct').textContent = `${promosPct}% da seleção atual`;

        // Desconto médio
        let avgDisc = 0;
        if (promosCount > 0) {
          const sum = promos.reduce((acc, p) => acc + (1 - p.price / p.list_price), 0);
          avgDisc = Math.round((sum / promosCount) * 100);
        }
        document.getElementById('stat-avg-disc').textContent = `${avgDisc}%`;

        // Maior desconto
        let maxDisc = 0;
        let maxProduct = null;
        filteredList.forEach(p => {
          if (p.list_price) {
            const pct = Math.round((1 - p.price / p.list_price) * 100);
            if (pct > maxDisc) {
              maxDisc = pct;
              maxProduct = p;
            }
          }
        });
        document.getElementById('stat-max-disc').textContent = `${maxDisc}%`;
        document.getElementById('stat-max-desc').textContent = maxProduct ? maxProduct.name.substring(0, 18) + '...' : 'Sem dados';
      }

      // Renderizar Visualização
      function render() {
        catalogContainer.innerHTML = '';
        const filtered = getFilteredProducts();
        updateDynamicMetrics(filtered);

        if (filtered.length === 0) {
          catalogContainer.innerHTML = `
            <div class="empty-state">
              <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
              <h3>Nenhum resultado encontrado</h3>
              <p>Tente alterar os termos de busca ou redefinir os filtros de lojas e tamanhos.</p>
              <button class="reset-btn" id="clear-filters-btn">Redefinir Filtros</button>
            </div>
          `;
          document.getElementById('clear-filters-btn')?.addEventListener('click', clearFilters);
          return;
        }

        if (state.layout === 'grid') {
          catalogContainer.className = '';
          const grid = document.createElement('div');
          grid.className = 'grid-view';
          
          const fragment = document.createDocumentFragment();
          filtered.forEach(p => {
            const discPct = p.list_price ? Math.round((1 - p.price / p.list_price) * 100) : 0;
            const isInsane = discPct >= 50;
            const cfg = SOURCE_CONFIG[p.source] || { label: p.source, color: '#fff', bg: '#222' };
            
            const card = document.createElement('div');
            card.className = `product-card ${isInsane ? 'insane-promo' : ''}`;
            card.dataset.sku = p.sku;
            card.dataset.source = p.source;
            
            // Gerar tamanhos compactos com destaque
            const sizeTags = (p.sizes || []).slice(0, 7).map(sz => {
              const isFav = sz === '42' || sz === '43';
              return `<span class="size-tag ${isFav ? 'fav' : ''}">${sz}</span>`;
            }).join('');
            
            const hasMoreSizes = (p.sizes || []).length > 7 ? '<span class="size-tag">+</span>' : '';

            const decodedName = safeDecode(p.name);
            const decodedBrand = safeDecode(p.brand);
            card.innerHTML = `
              <div class="card-image-wrapper">
                <img loading="lazy" src="${p.image || ''}" alt="${escape(decodedName)}">
                <div class="card-badges">
                  ${discPct > 0 ? `<span class="discount-badge">-${discPct}%</span>` : '<span></span>'}
                  <span class="store-badge" style="background:${cfg.color}">${cfg.label}</span>
                </div>
              </div>
              <div class="card-body">
                <div class="product-brand">${escape(decodedBrand || 'Streetwear')}</div>
                <div class="product-title" title="${escape(decodedName)}">${escape(decodedName)}</div>
                <div class="card-prices">
                  <span class="price-now">${fmtBRL(p.price)}</span>
                  ${p.list_price && p.list_price > p.price ? `<span class="price-old">${fmtBRL(p.list_price)}</span>` : ''}
                </div>
                ${p.sizes && p.sizes.length > 0 ? `
                  <div class="card-sizes">
                    <div class="sizes-title">Tamanhos Disp.</div>
                    <div class="sizes-list">
                      ${sizeTags}
                      ${hasMoreSizes}
                    </div>
                  </div>
                ` : ''}
              </div>
            `;
            
            card.addEventListener('click', () => openDetailModal(p));
            fragment.appendChild(card);
          });
          grid.appendChild(fragment);
          catalogContainer.appendChild(grid);
        } else {
          // Visualização em Tabela
          catalogContainer.className = 'list-view-container';
          const table = document.createElement('table');
          
          table.innerHTML = `
            <thead>
              <tr>
                <th style="width: 50px;">Img</th>
                <th data-col="source">Loja</th>
                <th data-col="brand">Marca</th>
                <th data-col="name">Produto</th>
                <th data-col="price" style="text-align:right;">Preço</th>
                <th data-col="list_price" style="text-align:right;">De</th>
                <th data-col="disc" style="text-align:center;">OFF</th>
                <th>Tamanhos</th>
              </tr>
            </thead>
            <tbody></tbody>
          `;
          
          const tbody = table.querySelector('tbody');
          filtered.forEach(p => {
            const discPct = p.list_price ? Math.round((1 - p.price / p.list_price) * 100) : 0;
            const cfg = SOURCE_CONFIG[p.source] || { label: p.source, color: '#fff' };
            const tr = document.createElement('tr');
            
            const decodedName = safeDecode(p.name);
            const decodedBrand = safeDecode(p.brand);
            tr.innerHTML = `
              <td><img src="${p.image || ''}" alt="" class="td-img"></td>
              <td><span style="color:${cfg.color}; font-weight:600;">${cfg.label}</span></td>
              <td>${escape(decodedBrand || '—')}</td>
              <td class="td-name">${escape(decodedName)}</td>
              <td class="td-price-now" style="text-align:right;">${fmtBRL(p.price)}</td>
              <td class="td-price-old" style="text-align:right;">${p.list_price && p.list_price > p.price ? fmtBRL(p.list_price) : '—'}</td>
              <td class="td-pct" style="text-align:center;">${discPct > 0 ? `-${discPct}%` : '—'}</td>
              <td style="color:var(--text-muted); font-size:11px;">${(p.sizes || []).join(', ')}</td>
            `;
            
            tr.addEventListener('click', () => openDetailModal(p));
            tbody.appendChild(tr);
          });
          
          catalogContainer.appendChild(table);
        }
      }

      // Evento: Busca
      searchInput.addEventListener('input', (e) => {
        state.search = e.target.value;
        render();
      });

      // Evento: Só Promoção
      promoOnlyCheck.addEventListener('change', (e) => {
        state.promoOnly = e.target.checked;
        render();
      });

      // Evento: Layout Grid
      layoutGridBtn.addEventListener('click', () => {
        state.layout = 'grid';
        layoutGridBtn.classList.add('active');
        layoutListBtn.classList.remove('active');
        render();
      });

      // Evento: Layout Lista
      layoutListBtn.addEventListener('click', () => {
        state.layout = 'list';
        layoutListBtn.classList.add('active');
        layoutGridBtn.classList.remove('active');
        render();
      });

      // Eventos: Checkboxes de Fontes
      sourceFiltersList.addEventListener('change', (e) => {
        if (e.target.classList.contains('pill-checkbox')) {
          const src = e.target.dataset.src;
          if (e.target.checked) {
            state.activeSources.add(src);
          } else {
            state.activeSources.delete(src);
          }
          render();
        }
      });

      // Handler de clique unificado para tamanho
      function handleSizeClick(e) {
        if (e.target.classList.contains('size-btn')) {
          const size = e.target.dataset.size;
          if (state.size === size) {
            state.size = null;
            state.activeSize = null;
          } else {
            state.size = size;
            state.activeSize = size;
          }
          renderSizesFilters();
          render();
        }
      }

      // Eventos: Tamanhos
      favSizesContainer.addEventListener('click', handleSizeClick);
      sizesFiltersGrid.addEventListener('click', handleSizeClick);

      // Evento: Abas de tamanho
      tabSneakersBtn.addEventListener('click', () => {
        tabSneakersBtn.classList.add('active');
        tabClothingBtn.classList.remove('active');
        state.activeSizeTab = 'sneakers';
        renderSizesFilters();
      });

      tabClothingBtn.addEventListener('click', () => {
        tabClothingBtn.classList.add('active');
        tabSneakersBtn.classList.remove('active');
        state.activeSizeTab = 'clothing';
        renderSizesFilters();
      });

      // Evento: Abrir/Fechar painel de outros tamanhos
      toggleAllSizesBtn.addEventListener('click', () => {
        const isOpen = allSizesPanel.classList.toggle('open');
        toggleAllSizesBtn.classList.toggle('panel-open', isOpen);
      });

      // Eventos: Faixas de Desconto
      discButtons.forEach(btn => {
        btn.addEventListener('click', () => {
          discButtons.forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          state.minDiscount = parseInt(btn.dataset.min);
          render();
        });
      });

      // Redefinir todos os filtros
      function clearFilters() {
        state.search = '';
        state.promoOnly = false;
        state.activeSize = null;
        state.size = null;
        state.minDiscount = 0;
        
        searchInput.value = '';
        promoOnlyCheck.checked = false;
        
        discButtons.forEach(b => b.classList.remove('active'));
        discButtons[0].classList.add('active');

        renderSizesFilters();
        
        const checks = sourceFiltersList.querySelectorAll('.pill-checkbox');
        checks.forEach(c => {
          c.checked = true;
          state.activeSources.add(c.dataset.src);
        });

        render();
      }

      // Configuração e Abertura do Modal de Detalhes
      const modal = document.getElementById('product-modal');
      const closeBtn = document.getElementById('modal-close-btn');

      function openDetailModal(product) {
        const discPct = product.list_price ? Math.round((1 - product.price / product.list_price) * 100) : 0;
        const cfg = SOURCE_CONFIG[product.source] || { label: product.source };
        
        const decodedName = safeDecode(product.name);
        const decodedBrand = safeDecode(product.brand);
        document.getElementById('modal-title-brand').textContent = `${escape(decodedBrand || 'STREETWEAR')} · ${cfg.label}`;
        document.getElementById('modal-img').src = product.image || '';
        document.getElementById('modal-title').textContent = decodedName;
        document.getElementById('modal-sku').textContent = `SKU/Código: ${product.sku}`;
        document.getElementById('modal-price').textContent = fmtBRL(product.price);
        document.getElementById('modal-price-old').style.display = product.list_price && product.list_price > product.price ? 'inline-block' : 'none';
        document.getElementById('modal-price-old').textContent = fmtBRL(product.list_price);
        document.getElementById('modal-discount').style.display = discPct > 0 ? 'inline-block' : 'none';
        document.getElementById('modal-discount').textContent = `-${discPct}% OFF`;
        document.getElementById('modal-available').textContent = product.available ? 'Em Estoque' : 'Esgotado';
        document.getElementById('modal-available').style.color = product.available ? '#2ed573' : '#ff4757';
        
        const stockRow = document.getElementById('modal-stock-row');
        if (product.stock_qty !== null && product.stock_qty !== undefined) {
          stockRow.style.display = 'block';
          document.getElementById('modal-stock').textContent = `${product.stock_qty} un.`;
        } else {
          stockRow.style.display = 'none';
        }

        const sizesContainer = document.getElementById('modal-sizes-list');
        sizesContainer.innerHTML = '';
        if (product.sizes && product.sizes.length > 0) {
          product.sizes.forEach(sz => {
            const isFav = sz === '42' || sz === '43';
            const span = document.createElement('span');
            span.className = `size-btn ${isFav ? 'fav active' : ''}`;
            span.style.cursor = 'default';
            span.textContent = sz;
            sizesContainer.appendChild(span);
          });
        } else {
          sizesContainer.innerHTML = '<span style="font-size:12px; font-style:italic; color:var(--text-dim)">Nenhum tamanho disponível</span>';
        }

        document.getElementById('modal-link').href = product.url;

        // Renderizar o gráfico de histórico
        renderHistoryChart(product);

        modal.classList.add('open');
      }

      function closeModal() {
        modal.classList.remove('open');
      }

      closeBtn.addEventListener('click', closeModal);
      modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
      });

      // Chart.js render engine
      function renderHistoryChart(product) {
        const ctx = document.getElementById('priceChart').getContext('2d');
        
        // Destruir gráfico anterior
        if (priceChartInstance) {
          priceChartInstance.destroy();
        }

        // Preparar dados históricos
        const history = product.history || [];
        
        // Se vazia, criar entrada base a partir do atual
        if (history.length === 0) {
          history.push({
            date: new Date(metadata.generated_at).toISOString().split('T')[0],
            price: product.price
          });
        }

        // Mapear datas e valores para o gráfico
        const labels = history.map(h => {
          const parts = h.date.split('-');
          return parts.length === 3 ? `${parts[2]}/${parts[1]}` : h.date;
        });
        const prices = history.map(h => h.price);

        // Preço atual
        const curPrice = product.price;

        priceChartInstance = new Chart(ctx, {
          type: 'line',
          data: {
            labels: labels,
            datasets: [{
              label: 'Preço (R$)',
              data: prices,
              borderColor: '#ff4757',
              borderWidth: 2,
              pointBackgroundColor: '#fff',
              pointBorderColor: '#ff4757',
              pointRadius: 4,
              pointHoverRadius: 6,
              fill: true,
              backgroundColor: 'rgba(255, 71, 87, 0.05)',
              tension: 0.1
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: function(context) {
                    return ` R$ ${context.parsed.y.toFixed(2).replace('.', ',')}`;
                  }
                }
              }
            },
            scales: {
              x: {
                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                ticks: { color: 'rgba(255, 255, 255, 0.4)', font: { size: 10 } }
              },
              y: {
                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                ticks: {
                  color: 'rgba(255, 255, 255, 0.4)',
                  font: { size: 10 },
                  callback: function(val) { return `R$ ${val}`; }
                }
              }
            }
          }
        });
      }

      // Render inicial
      clearFilters();
    })();
  </script>
</body>
</html>
"""


def compress_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Comprime o histórico mantendo apenas o ponto inicial, o ponto final e os

    pontos onde o preço ou list_price realmente mudaram. Isso reduz
    drasticamente o payload JSON mantendo a precisão das oscilações.
    """
    if len(history) <= 2:
        return history

    compressed = [history[0]]
    for i in range(1, len(history) - 1):
        prev = history[i - 1]
        curr = history[i]
        nxt = history[i + 1]

        # Mantém se houver qualquer alteração de preço
        price_changed = (
            abs(curr["price"] - prev["price"]) > 0.001
            or abs(curr["price"] - nxt["price"]) > 0.001
        )
        list_price_changed = False

        curr_list = curr.get("list_price")
        prev_list = prev.get("list_price")
        nxt_list = nxt.get("list_price")

        if curr_list is not None and prev_list is not None:
            list_price_changed = list_price_changed or abs(curr_list - prev_list) > 0.001
        if curr_list is not None and nxt_list is not None:
            list_price_changed = list_price_changed or abs(curr_list - nxt_list) > 0.001

        # Também mantém se um dos valores de list_price era Nulo e passou a existir ou vice-versa
        if (curr_list is None) != (prev_list is None) or (curr_list is None) != (nxt_list is None):
            list_price_changed = True

        if price_changed or list_price_changed:
            compressed.append(curr)

    compressed.append(history[-1])
    return compressed


def generate_dashboard_data(conn: Any) -> Dict[str, Any]:
    """Extrai e consolida produtos ativos e histórico de preços a partir do SQLite."""
    # Obter o último snapshot de cada produto
    latest_rows = conn.execute("""
        WITH latest AS (
            SELECT source, sku, list_price, price, available, sizes, stock_qty, observed_at,
                   ROW_NUMBER() OVER (PARTITION BY source, sku ORDER BY observed_at DESC) AS rn
            FROM price_history
        )
        SELECT p.source, p.sku, p.name, p.url, p.image, p.brand,
               l.list_price, l.price, l.available, l.sizes, l.stock_qty, l.observed_at
        FROM latest l
        JOIN products p USING (source, sku)
        WHERE l.rn = 1
        ORDER BY p.source, p.sku
    """).fetchall()

    # Obter todo o histórico de preços
    history_rows = conn.execute("""
        SELECT source, sku, observed_at, price, list_price
        FROM price_history
        ORDER BY source, sku, observed_at ASC
    """).fetchall()

    # Mapear histórico por produto
    history_map: Dict[str, List[Dict[str, Any]]] = {}
    for h in history_rows:
        key = f"{h['source']}:{h['sku']}"
        if key not in history_map:
            history_map[key] = []
        # Reduzir timestamp para data (YYYY-MM-DD)
        date_str = h["observed_at"].split("T")[0]
        history_map[key].append({
            "date": date_str,
            "price": float(h["price"]),
            "list_price": float(h["list_price"]) if h["list_price"] is not None else None
        })

    products = []
    for r in latest_rows:
        key = f"{r['source']}:{r['sku']}"
        raw_history = history_map.get(key, [])
        compressed = compress_history(raw_history)

        sizes_list = []
        if r["sizes"]:
            sizes_list = [s.strip() for s in r["sizes"].split(",") if s.strip()]

        products.append({
            "source": r["source"],
            "sku": r["sku"],
            "name": r["name"],
            "url": r["url"],
            "image": r["image"],
            "brand": r["brand"],
            "price": float(r["price"]),
            "list_price": float(r["list_price"]) if r["list_price"] is not None else None,
            "available": bool(r["available"]),
            "sizes": sizes_list,
            "stock_qty": int(r["stock_qty"]) if r["stock_qty"] is not None else None,
            "observed_at": r["observed_at"],
            "history": compressed
        })

    source_status = [dict(row) for row in latest_source_runs(conn)]

    return {
        "products": products,
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_products": len(products),
            "source_status": source_status
        }
    }


def write_dashboard(conn: Any, output_path: Path) -> None:
    """Consolida os dados no template HTML e grava no arquivo de saída."""
    data = generate_dashboard_data(conn)
    data_json = json.dumps(data, ensure_ascii=False)
    
    # Injetar dados no template
    source_config_json = json.dumps(dashboard_source_config(), ensure_ascii=False)
    html_content = (
        HTML_TEMPLATE
        .replace("%%PRODUCTS_JSON%%", data_json)
        .replace("%%SOURCE_CONFIG_JSON%%", source_config_json)
    )
    
    # Criar diretórios pais se não existirem
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Gravar arquivo final com codificação utf-8
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
