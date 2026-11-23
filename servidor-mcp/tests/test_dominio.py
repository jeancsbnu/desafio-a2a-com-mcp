"""Regras de dominio nas bordas: janela, duracao, sobreposicao e alternativas.

    cd servidor-mcp
    uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import unittest

from servidor_mcp import dominio
from servidor_mcp.dominio import RegraViolada

DIA = "2026-11-20"


def h(hora: str, fuso: str = "-03:00") -> str:
    return f"{DIA}T{hora}:00{fuso}"


class ComReservasIsoladas(unittest.TestCase):
    """Cada teste comeca com as reservas de `dados/reservas.json` e nada mais."""

    def setUp(self) -> None:
        self._originais = list(dominio.RESERVAS)

    def tearDown(self) -> None:
        dominio.RESERVAS[:] = self._originais


class TestValidar(ComReservasIsoladas):
    def assertViola(self, mensagem: str, sala: str, inicio: str, fim: str) -> None:
        with self.assertRaises(RegraViolada) as erro:
            dominio.validar(sala, inicio, fim)
        self.assertEqual(str(erro.exception), mensagem)

    def test_sala_inexistente(self) -> None:
        self.assertViola("Sala inexistente: sala-delorean", "sala-delorean", h("09:00"), h("10:00"))

    def test_intervalo_invertido_e_vazio(self) -> None:
        mensagem = "Intervalo invalido: fim deve ser posterior a inicio"
        self.assertViola(mensagem, "sala-aquario", h("10:00"), h("09:00"))
        self.assertViola(mensagem, "sala-aquario", h("10:00"), h("10:00"))

    def test_janela_nas_bordas(self) -> None:
        mensagem = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
        dominio.validar("sala-aquario", h("08:00"), h("09:00"))
        dominio.validar("sala-aquario", h("19:00"), h("20:00"))
        self.assertViola(mensagem, "sala-aquario", h("07:59"), h("09:00"))
        self.assertViola(mensagem, "sala-aquario", h("19:30"), h("20:01"))

    def test_janela_convertida_para_sao_paulo(self) -> None:
        # 11:00Z e 08:00 em -03:00; 10:59Z e 07:59.
        dominio.validar("sala-aquario", h("11:00", "Z"), h("12:00", "+00:00"))
        self.assertViola(
            "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00",
            "sala-aquario",
            h("10:59", "+00:00"),
            h("12:00", "+00:00"),
        )

    def test_duracao_nas_bordas(self) -> None:
        dominio.validar("sala-aquario", h("09:00"), h("11:00"))
        self.assertViola(
            "Duracao acima do limite: a politica permite no maximo 2 horas", "sala-aquario", h("09:00"), h("11:01")
        )

    def test_data_ilegivel_ou_sem_fuso(self) -> None:
        with self.assertRaisesRegex(RegraViolada, "^Data invalida: amanha"):
            dominio.validar("sala-aquario", "amanha", h("10:00"))
        with self.assertRaisesRegex(RegraViolada, "^Data sem fuso horario"):
            dominio.validar("sala-aquario", f"{DIA}T09:00:00", h("10:00"))


class TestConflitosEAlternativas(ComReservasIsoladas):
    def intervalo(self, inicio: str, fim: str):
        return dominio.validar("sala-garagem", h(inicio), h(fim))[1:]

    def test_sobreposicao_semiaberta(self) -> None:
        dominio.criar_reserva("sala-garagem", h("10:00"), h("11:00"), "Ocupante")
        self.assertEqual(dominio.conflitos("sala-garagem", *self.intervalo("09:00", "10:00")), [])
        self.assertEqual(dominio.conflitos("sala-garagem", *self.intervalo("11:00", "12:00")), [])
        self.assertEqual(len(dominio.conflitos("sala-garagem", *self.intervalo("10:59", "11:30"))), 1)
        self.assertEqual(len(dominio.conflitos("sala-garagem", *self.intervalo("09:30", "11:30"))), 1)

    def test_alternativas_por_capacidade_e_id_no_maximo_tres(self) -> None:
        aquario, ini, fi = dominio.validar("sala-aquario", h("09:00"), h("10:00"))
        # Capacidade >= 4: porao (6), fusca e garagem (12, desempate por id), mirante (20) fica de fora.
        self.assertEqual(dominio.alternativas(aquario, ini, fi), ["sala-porao", "sala-fusca", "sala-garagem"])

    def test_alternativas_excluem_a_pedida_ocupadas_e_menores(self) -> None:
        dominio.criar_reserva("sala-fusca", h("14:00"), h("15:00"), "Intruso")
        garagem, ini, fi = dominio.validar("sala-garagem", h("14:00"), h("15:00"))
        self.assertEqual(dominio.alternativas(garagem, ini, fi), ["sala-mirante"])

    def test_sem_alternativas(self) -> None:
        mirante, ini, fi = dominio.validar("sala-mirante", h("11:00"), h("12:00"))
        self.assertEqual(dominio.alternativas(mirante, ini, fi), [])

    def test_wire_de_exemplo_garagem_14h(self) -> None:
        garagem, ini, fi = dominio.validar("sala-garagem", "2026-11-03T14:00:00-03:00", "2026-11-03T15:00:00-03:00")
        self.assertEqual([r["id"] for r in dominio.conflitos("sala-garagem", ini, fi)], ["res-0001"])
        self.assertEqual(dominio.alternativas(garagem, ini, fi), ["sala-fusca", "sala-mirante"])


class TestReservasEPolitica(ComReservasIsoladas):
    def test_ids_sequenciais(self) -> None:
        primeira = dominio.criar_reserva("sala-aquario", h("09:00"), h("10:00"), "Doc")
        segunda = dominio.criar_reserva("sala-aquario", h("10:00"), h("11:00"), "Doc")
        self.assertEqual((primeira["id"], segunda["id"]), ("res-0003", "res-0004"))

    def test_versao_da_politica(self) -> None:
        self.assertEqual(dominio.politica_versao(), "2026-11-01")


if __name__ == "__main__":
    unittest.main()
