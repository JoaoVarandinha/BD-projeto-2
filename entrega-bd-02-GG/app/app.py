#!/usr/bin/python3
# Copyright (c) BDist Development Team
# Distributed under the terms of the Modified BSD License.
import os
from logging.config import dictConfig

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from psycopg.rows import namedtuple_row
from psycopg_pool import ConnectionPool

dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s:%(lineno)s - %(funcName)20s(): %(message)s",
            }
        },
        "handlers": {
            "wsgi": {
                "class": "logging.StreamHandler",
                "stream": "ext://flask.logging.wsgi_errors_stream",
                "formatter": "default",
            }
        },
        "root": {"level": "INFO", "handlers": ["wsgi"]},
    }
)

RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

app = Flask(__name__)
app.config.from_prefixed_env()
log = app.logger
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=RATELIMIT_STORAGE_URI,
)

# Use the DATABASE_URL environment variable if it exists, otherwise use the default.
# Use the format postgres://username:password@hostname/database_name to connect to the database.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://app:app@postgres/app")

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    kwargs={
        "autocommit": True,  # If True don’t start transactions automatically.
        "row_factory": namedtuple_row,
    },
    min_size=4,
    max_size=10,
    open=True,
    # check=ConnectionPool.check_connection,
    name="postgres_pool",
    timeout=5,
)


@app.route("/zona/<zona>", methods=("GET",))
@limiter.limit("1 per second")
def zona_index(zona):
    """Mostra todos os recintos de <zona>, contendo
    o número do recinto e as espécies nele contidas,
    indicando, para cada espécie, os nomes científico
    e comum, e o número de animais da espécie que estão
    no recinto."""

    with pool.connection() as conn:
        with conn.cursor() as cur:
            recintos = cur.execute(
                """
                SELECT id_recinto, nome_cientifico, nome_comum, COUNT(DISTINCT id_animal) as numero_animais
                FROM recinto
                    JOIN animal USING (id_recinto)
                    JOIN especie USING (nome_cientifico)
                WHERE id_zona = %(zona)s
                GROUP BY id_recinto, nome_cientifico, nome_comum
                ORDER BY id_recinto
                """,
                {"zona": zona},
            ).fetchall()
            log.debug(f"Found {cur.rowcount} rows.")

    return jsonify(recintos), 200

@app.route(
    "/recinto/<recinto>/voto/<bilhete>",
    methods=(
        "POST",
    ),
)
def recinto_voto_save(recinto, bilhete):
    """Assinala o voto do <bilhete> no <recinto>,
    atualizando as tabelas bilhete (marcando 'votou' TRUE)
    e recinto (incrementando os 'votos'). Devolve mensagem
    de erro informativa e diferenciada (e não assinala o voto)
    se o <bilhete> já tinha 'votou' = TRUE ou se o <recinto>
    não está contido numa zona a que o <bilhete> tinha acesso."""

    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                with conn.transaction():
                    ticket = cur.execute(
                        """
                        SELECT votou
                        FROM bilhete
                        WHERE bid = %(bilhete)s;
                        """,
                        {"bilhete":bilhete},
                    ).fetchone()

                    if ticket is None:
                        return jsonify({"message": "Ticket not found.", "status": "error"}), 404
                    if ticket.votou:
                        return jsonify({"message": "Ticket already voted", "status": "error"}), 400
                    
                    enclosure_check = cur.execute(
                        """
                        SELECT 1 
                        FROM acesso
                        JOIN recinto USING (id_zona)
                        WHERE bid = %(bilhete)s
                        AND id_recinto = %(recinto)s;
                        """,
                        {"bilhete":bilhete,"recinto":recinto},
                    ).fetchone()

                    if enclosure_check is None:
                        return jsonify({"message": "Ticket must have access to enclosure", "status": "error"}), 400

                    cur.execute(
                        """
                        UPDATE bilhete
                        SET votou = TRUE
                        WHERE bid = %(bilhete)s;
                        """,
                        {"bilhete": bilhete},
                    )
                    cur.execute(
                        """
                        UPDATE recinto
                        SET votos = votos + 1
                        WHERE id_recinto = %(recinto)s;
                        """,
                        {"recinto": recinto},
                    )

            except Exception as e:
                return jsonify({"message": str(e), "status": "error"}), 500
            
            return "", 204


@app.route(
    "/venda/",
    methods=(
        "POST",
    ),
)
def venda_save():
    """Executa uma venda de um ou mais bilhetes,
    populando as tabelas venda, bilhete e acesso.
    INPUT: NIF do cliente [opcional] mais lista de bilhetes,
    em que cada bilhete consiste numa lista de zonas de acesso,
    e um desconto [opcional]. OUTPUT: O preço total da venda,
    e a lista dos bilhetes da venda com o número e preço de cada um."""

    body = request.json

    if not body:
        return jsonify({"message": "Request with no body", "status": "error"}), 400

    nif = body.get("nif")
    tickets = body.get("bilhetes")

    if not tickets:
        return jsonify({"message": "Request missing tickets", "status": "error"}), 400
    for ticket in tickets:
        if not ticket.get("zonas"):
            return jsonify({"message": "Each ticket must have zones", "status": "error"}), 400
    
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                with conn.transaction():
                    sale = cur.execute(
                        """
                        INSERT INTO venda(data_hora, nif_cliente)
                        VALUES (NOW(),%(nif)s)
                        RETURNING no_venda;
                        """,
                        {"nif":nif},
                    ).fetchone()

                    ticket_list = []
                    total_price = 0

                    #Usar um hash map para minimizar os Selects feitos
                    all_zones = list({z for t in tickets for z in t["zonas"]})

                    zone_prices = cur.execute(
                        """SELECT id_zona, preco
                            FROM zona
                            WHERE id_zona = ANY(%(all_zones)s);
                        """,
                        {"all_zones":all_zones},
                    ).fetchall()

                    price_map = {row.id_zona : float(row.preco) for row in zone_prices}
                    for ticket in tickets:
                        ticket_price = 0
                        discount = ticket.get("desconto",0)
                        bid = cur.execute(
                            """
                            INSERT INTO bilhete(desconto, no_venda)
                            VALUES (%(discount)s,%(sale)s)
                            RETURNING bid;
                            """,
                            {"discount":discount,"sale":sale.no_venda},
                        ).fetchone()
                        
                        zones = ticket.get("zonas")
                        for zone in zones:
                            cur.execute(
                                """
                                INSERT INTO acesso(bid,id_zona)
                                VALUES (%(bid)s,%(zone)s)
                                """,
                                {"bid":bid.bid,"zone":zone},
                            )
                            ticket_price += price_map.get(zone)

                        if discount > 0:
                            ticket_price *= 1 - discount
                        total_price += ticket_price
                        ticket_list.append({"bid":bid.bid,"preco":ticket_price})
                    return jsonify({"preco_total":total_price,"bilhetes":ticket_list})
                
            except Exception as e:
                return jsonify({"message":str(e),"status":"error"}), 400


if __name__ == "__main__":
    app.run()
