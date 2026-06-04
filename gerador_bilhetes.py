import itertools
from datetime import date, timedelta

# Configuração de datas
start_date = date(2026, 1, 1)
end_date = date(2026, 6, 11)

# Preparar todas as combinações de Zonas (mínimo 3 zonas)
zones = list(range(1, 11))
all_combos = []
for r in range(3, 11):
    all_combos.extend(list(itertools.combinations(zones, r)))

# Leve viés na especialidade (zonas 9 e 10)
specialty_combos = [c for c in all_combos if 9 in c or 10 in c]
rotation_pool = all_combos + specialty_combos
total_combos = len(rotation_pool)

print("A gerar o ficheiro inserts_vendas.sql com percentagens dinâmicas de Acesso Total...")

with open('inserts_vendas.sql', 'w', encoding='utf-8') as f:
    # Limpeza prévia
    f.write("DELETE FROM acesso;\n")
    f.write("DELETE FROM bilhete;\n")
    f.write("DELETE FROM venda;\n\n")

    no_venda = 1
    bid = 1
    combo_idx = 0
    curr_date = start_date
    
    while curr_date <= end_date:
        is_weekend = curr_date.weekday() >= 5
        num_tickets = 4000 if is_weekend else 1000
        
        # ---------------------------------------------------------------------
        # NOVA LÓGICA: Variação Determinista da Percentagem de Acesso Total
        # Garante o mínimo de 2% pedido no enunciado, mas flutua até aos ~9%
        # consoante a matemática do dia, mês e dia da semana.
        # ---------------------------------------------------------------------
        base_percent = 0.02
        month_variance = (curr_date.month % 3) * 0.015     # 0% a 3%
        day_variance = (curr_date.day % 5) * 0.008         # 0% a 3.2%
        weekday_variance = (curr_date.weekday() % 4) * 0.005 # 0% a 1.5%
        
        total_percent = base_percent + month_variance + day_variance + weekday_variance
        num_full_access = int(num_tickets * total_percent)
        
        vendas = []
        bilhetes = []
        acessos = []
        
        for i in range(num_tickets):
            hour = 9 + (i % 10)
            minute = i % 60
            data_hora = f"{curr_date.isoformat()} {hour:02d}:{minute:02d}:00"
            
            nif_cliente = f"'{100000000 + (no_venda % 899999999)}'" if no_venda % 5 == 0 else "NULL"
            vendas.append(f"({no_venda}, '{data_hora}', {nif_cliente})")
            
            desconto = "0.50" if i % 2 == 0 else "0.00"
            votou = "TRUE" if (i % 4 != 0) else "FALSE" 
            bilhetes.append(f"({bid}, {desconto}, {votou}, {no_venda})")
            
            # Aplica os bilhetes globais flutuantes primeiro
            if i < num_full_access:
                chosen_combo = zones
            else:
                chosen_combo = rotation_pool[combo_idx % total_combos]
                combo_idx += 1
            
            for z in chosen_combo:
                acessos.append(f"({bid}, {z})")
            
            no_venda += 1
            bid += 1
        
        # Commits Diários
        f.write(f"-- Inserts para o dia {curr_date}\n")
        f.write("BEGIN;\n")
        f.write("INSERT INTO venda (no_venda, data_hora, nif_cliente) VALUES\n" + ",\n".join(vendas) + ";\n")
        f.write("INSERT INTO bilhete (bid, desconto, votou, no_venda) VALUES\n" + ",\n".join(bilhetes) + ";\n")
        f.write("INSERT INTO acesso (bid, id_zona) VALUES\n" + ",\n".join(acessos) + ";\n")
        f.write("COMMIT;\n\n")
        
        curr_date += timedelta(days=1)
        
    f.write("-- Atualizar as sequences\n")
    f.write("SELECT setval('venda_no_venda_seq', (SELECT COALESCE(max(no_venda), 1) FROM venda));\n")
    f.write("SELECT setval('bilhete_bid_seq', (SELECT COALESCE(max(bid), 1) FROM bilhete));\n\n")
    
    # LÓGICA DE VOTOS: Distribuição em Escada com Mínimos Garantidos
    f.write("-- Distribuir os votos com declive realista garantindo os mínimos\n")
    f.write("BEGIN;\n")
    f.write('''UPDATE recinto SET votos = 0;

WITH ticket_votos AS (
    SELECT a.bid, a.id_zona,
           row_number() OVER (PARTITION BY a.bid ORDER BY a.id_zona) as seq,
           count(*) OVER (PARTITION BY a.bid) as total_zonas
    FROM acesso a
    JOIN bilhete b ON a.bid = b.bid
    WHERE b.votou = TRUE
),
chosen_votes AS (
    SELECT bid, id_zona
    FROM ticket_votos
    WHERE seq = (bid % total_zonas) + 1
),
zone_counts AS (
    SELECT id_zona, count(*) as votos_zona
    FROM chosen_votes
    GROUP BY id_zona
),
global_counts AS (
    SELECT CAST(CEIL(count(*) * 0.001) AS INTEGER) + 1 as min_votos
    FROM chosen_votes
),
recinto_rn AS (
    SELECT id_recinto, id_zona,
           row_number() OVER (PARTITION BY id_zona ORDER BY id_recinto) as rn,
           count(*) OVER (PARTITION BY id_zona) as num_recintos
    FROM recinto
),
vote_math AS (
    SELECT r.id_recinto, r.id_zona, r.rn,
           g.min_votos,
           CASE WHEN z.votos_zona > (r.num_recintos * g.min_votos) 
                THEN z.votos_zona - (r.num_recintos * g.min_votos) 
                ELSE 0 END as pool,
           (r.num_recintos - r.rn + 1) as weight,
           (r.num_recintos * (r.num_recintos + 1)) / 2 as total_weight
    FROM recinto_rn r
    JOIN zone_counts z ON r.id_zona = z.id_zona
    CROSS JOIN global_counts g
),
shares AS (
    SELECT id_recinto, id_zona, rn, min_votos, pool,
           (pool * weight) / total_weight as share
    FROM vote_math
),
final_distribution AS (
    SELECT id_recinto, id_zona, rn,
           min_votos + share as base_calc,
           pool - sum(share) OVER (PARTITION BY id_zona) as remainder
    FROM shares
)
UPDATE recinto r SET votos = f.base_calc + CASE WHEN f.rn = 1 THEN f.remainder ELSE 0 END
FROM final_distribution f
WHERE r.id_recinto = f.id_recinto;
''')
    f.write("COMMIT;\n")

print("Ficheiro 'inserts_vendas.sql' criado. Variação orgânica no Acesso Total e votos em escada aplicados.")