"""Selectolax-based HTML parsers for the FIS free-lot registry."""

from fis_monitor.infra.parsers.detail_parser import SelectolaxDetailParser
from fis_monitor.infra.parsers.list_parser import SelectolaxListParser

__all__ = ["SelectolaxDetailParser", "SelectolaxListParser"]
