"""Marcadores de laboratorio: rangos de referencia y a que peptidos aplican.

Los rangos son los de uso comun en adultos y VARIAN entre laboratorios: se usan
solo para colorear un valor como dentro o fuera de rango, nunca para concluir
nada. La fuente que manda siempre es el rango impreso en la hoja del paciente.

`peptides` lista los patrones de compuesto para los que ese marcador es
pertinente. Es lo que acota la herramienta: solo mostramos e interpretamos los
marcadores que tienen que ver con lo que el cliente compro o planea comprar.
"""

# Familias de compuestos del catalogo, por patron de nombre.
FAMILIES = {
    'incretinas': r'tirzepat|retatrut|semaglut|cagrilint|mazdut|survodut|liraglut|dulaglut|reta|sema|tirze|cagri',
    'gh': r'cjc|ipamorel|sermorel|tesamorel|ghrp|hexarel|igf|hgh|mgf|follistat',
    'reparacion': r'bpc|tb-?500|ghk|kpv|glow|klow|ara-?290|pnc',
    'metabolico': r'nad|mots|ss-?31|amino-?1mq|aicar|humanin|slu-?pp|bam-?15|aod|carnit',
    'inmune': r'thymos|thymal|ll-?37|timosin|timal|vilon|epithal|epitalon',
    'hormonal': r'pt-?141|kisspept|gonadorel|hcg|hmg|oxitocin|oxytocin|melanotan|enclomif|clomif',
    'cognitivo': r'semax|selank|dihexa|p21|cerebrol|pinealon|dsip|adamax',
    'antioxidante': r'glutat|glutath|vitamina|matrixyl|ahk|snap',
}

# clave -> {label, unit, low, high, sexed, group, plain, peptides}
# `sexed` = {'m': (low, high), 'f': (low, high)} cuando el rango depende del sexo.
MARKERS = [
    # --- Metabolico / glucosa ---
    {'key': 'glucosa', 'label': 'Glucosa en ayuno', 'unit': 'mg/dL', 'low': 70, 'high': 99, 'group': 'Metabolismo',
     'plain': 'Cuanta azucar circula en la sangre despues de varias horas sin comer.',
     'peptides': ['incretinas', 'gh', 'metabolico']},
    {'key': 'hba1c', 'label': 'Hemoglobina glucosilada (HbA1c)', 'unit': '%', 'low': 4.0, 'high': 5.6, 'group': 'Metabolismo',
     'plain': 'El promedio aproximado de tu glucosa en los ultimos dos a tres meses.',
     'peptides': ['incretinas', 'gh', 'metabolico']},
    {'key': 'insulina', 'label': 'Insulina en ayuno', 'unit': 'uUI/mL', 'low': 2.6, 'high': 24.9, 'group': 'Metabolismo',
     'plain': 'La hormona que mete la glucosa a las celulas. Muy alta suele indicar resistencia.',
     'peptides': ['incretinas', 'gh', 'metabolico']},
    {'key': 'homa_ir', 'label': 'HOMA-IR', 'unit': '', 'low': 0.5, 'high': 2.0, 'group': 'Metabolismo',
     'plain': 'Indice calculado con glucosa e insulina que estima resistencia a la insulina.',
     'peptides': ['incretinas', 'metabolico']},
    {'key': 'acido_urico', 'label': 'Acido urico', 'unit': 'mg/dL', 'sexed': {'m': (3.4, 7.0), 'f': (2.4, 6.0)}, 'group': 'Metabolismo',
     'plain': 'Producto de desecho del metabolismo de las purinas.',
     'peptides': ['metabolico', 'incretinas']},

    # --- Lipidos ---
    {'key': 'colesterol_total', 'label': 'Colesterol total', 'unit': 'mg/dL', 'low': 0, 'high': 200, 'group': 'Lipidos',
     'plain': 'La suma de todas las fracciones de colesterol.',
     'peptides': ['incretinas', 'metabolico', 'gh']},
    {'key': 'ldl', 'label': 'Colesterol LDL', 'unit': 'mg/dL', 'low': 0, 'high': 100, 'group': 'Lipidos',
     'plain': 'La fraccion que se asocia a acumulacion en las arterias.',
     'peptides': ['incretinas', 'metabolico']},
    {'key': 'hdl', 'label': 'Colesterol HDL', 'unit': 'mg/dL', 'sexed': {'m': (40, 200), 'f': (50, 200)}, 'group': 'Lipidos',
     'plain': 'La fraccion que retira colesterol de los tejidos. Aqui mas alto es mejor.',
     'peptides': ['incretinas', 'metabolico']},
    {'key': 'trigliceridos', 'label': 'Trigliceridos', 'unit': 'mg/dL', 'low': 0, 'high': 150, 'group': 'Lipidos',
     'plain': 'La grasa que circula en sangre. Sube mucho con azucar y alcohol.',
     'peptides': ['incretinas', 'metabolico']},

    # --- Higado ---
    {'key': 'alt', 'label': 'ALT (TGP)', 'unit': 'U/L', 'sexed': {'m': (7, 55), 'f': (7, 45)}, 'group': 'Higado',
     'plain': 'Enzima del higado. Sube cuando las celulas hepaticas se irritan.',
     'peptides': ['incretinas', 'metabolico', 'reparacion', 'antioxidante']},
    {'key': 'ast', 'label': 'AST (TGO)', 'unit': 'U/L', 'sexed': {'m': (8, 48), 'f': (8, 43)}, 'group': 'Higado',
     'plain': 'Otra enzima que comparten higado y musculo. Sube tambien tras entrenar fuerte.',
     'peptides': ['incretinas', 'metabolico', 'reparacion', 'antioxidante']},
    {'key': 'ggt', 'label': 'GGT', 'unit': 'U/L', 'sexed': {'m': (8, 61), 'f': (5, 36)}, 'group': 'Higado',
     'plain': 'Enzima sensible al alcohol y a la via biliar.',
     'peptides': ['metabolico', 'antioxidante']},
    {'key': 'bilirrubina_total', 'label': 'Bilirrubina total', 'unit': 'mg/dL', 'low': 0.1, 'high': 1.2, 'group': 'Higado',
     'plain': 'Pigmento que procesa el higado al reciclar globulos rojos.',
     'peptides': ['antioxidante', 'metabolico']},

    # --- Rinon ---
    {'key': 'creatinina', 'label': 'Creatinina', 'unit': 'mg/dL', 'sexed': {'m': (0.74, 1.35), 'f': (0.59, 1.04)}, 'group': 'Rinon',
     'plain': 'Desecho muscular que filtra el rinon. Sube con masa muscular alta, no solo con dano renal.',
     'peptides': ['incretinas', 'metabolico', 'gh']},
    {'key': 'egfr', 'label': 'Filtrado glomerular (eGFR)', 'unit': 'mL/min/1.73m2', 'low': 90, 'high': 200, 'group': 'Rinon',
     'plain': 'Estimacion de que tan bien filtran los rinones.',
     'peptides': ['incretinas', 'metabolico']},

    # --- Pancreas ---
    {'key': 'lipasa', 'label': 'Lipasa', 'unit': 'U/L', 'low': 13, 'high': 60, 'group': 'Pancreas',
     'plain': 'Enzima pancreatica. Es el marcador que mas se vigila con la via GLP-1.',
     'peptides': ['incretinas']},
    {'key': 'amilasa', 'label': 'Amilasa', 'unit': 'U/L', 'low': 30, 'high': 110, 'group': 'Pancreas',
     'plain': 'Otra enzima pancreatica, menos especifica que la lipasa.',
     'peptides': ['incretinas']},

    # --- Eje somatotropo ---
    {'key': 'igf1', 'label': 'IGF-1', 'unit': 'ng/mL', 'low': 90, 'high': 300, 'group': 'Eje hormonal',
     'plain': 'El mensajero por el que actua la hormona de crecimiento. Es el marcador clave de esa via.',
     'peptides': ['gh']},
    {'key': 'prolactina', 'label': 'Prolactina', 'unit': 'ng/mL', 'sexed': {'m': (4, 15.2), 'f': (4.8, 23.3)}, 'group': 'Eje hormonal',
     'plain': 'Hormona hipofisaria. Algunos secretagogos poco selectivos la mueven.',
     'peptides': ['gh', 'hormonal']},
    {'key': 'cortisol', 'label': 'Cortisol matutino', 'unit': 'ug/dL', 'low': 6.2, 'high': 19.4, 'group': 'Eje hormonal',
     'plain': 'La hormona del estres. Se mide en la manana porque cambia durante el dia.',
     'peptides': ['gh', 'cognitivo']},

    # --- Tiroides ---
    {'key': 'tsh', 'label': 'TSH', 'unit': 'uUI/mL', 'low': 0.4, 'high': 4.5, 'group': 'Tiroides',
     'plain': 'La senal que manda el cerebro a la tiroides. Sube cuando la tiroides trabaja de menos.',
     'peptides': ['gh', 'incretinas', 'metabolico']},
    {'key': 't4_libre', 'label': 'T4 libre', 'unit': 'ng/dL', 'low': 0.8, 'high': 1.8, 'group': 'Tiroides',
     'plain': 'La hormona tiroidea disponible en sangre.',
     'peptides': ['gh', 'metabolico']},

    # --- Eje gonadal ---
    {'key': 'testosterona_total', 'label': 'Testosterona total', 'unit': 'ng/dL', 'sexed': {'m': (300, 1000), 'f': (15, 70)}, 'group': 'Eje hormonal',
     'plain': 'La principal hormona androgenica.',
     'peptides': ['hormonal', 'gh']},
    {'key': 'estradiol', 'label': 'Estradiol', 'unit': 'pg/mL', 'sexed': {'m': (10, 40), 'f': (30, 400)}, 'group': 'Eje hormonal',
     'plain': 'Principal estrogeno. En mujeres cambia mucho segun el dia del ciclo.',
     'peptides': ['hormonal']},
    {'key': 'lh', 'label': 'LH', 'unit': 'UI/L', 'sexed': {'m': (1.7, 8.6), 'f': (1.0, 95.6)}, 'group': 'Eje hormonal',
     'plain': 'Hormona que ordena a las gonadas producir. En mujeres depende del ciclo.',
     'peptides': ['hormonal']},
    {'key': 'fsh', 'label': 'FSH', 'unit': 'UI/L', 'sexed': {'m': (1.5, 12.4), 'f': (1.0, 130.0)}, 'group': 'Eje hormonal',
     'plain': 'La otra hormona del eje gonadal. En mujeres depende del ciclo.',
     'peptides': ['hormonal']},
    {'key': 'shbg', 'label': 'SHBG', 'unit': 'nmol/L', 'sexed': {'m': (10, 57), 'f': (18, 144)}, 'group': 'Eje hormonal',
     'plain': 'Proteina que transporta hormonas sexuales y define cuanta queda libre.',
     'peptides': ['hormonal']},

    # --- Inflamacion e inmunidad ---
    {'key': 'pcr', 'label': 'PCR ultrasensible', 'unit': 'mg/L', 'low': 0, 'high': 3.0, 'group': 'Inflamacion',
     'plain': 'Marcador general de inflamacion. Sube con infecciones, lesiones y ejercicio intenso reciente.',
     'peptides': ['reparacion', 'inmune', 'metabolico']},
    {'key': 'vsg', 'label': 'Velocidad de sedimentacion (VSG)', 'unit': 'mm/h', 'sexed': {'m': (0, 15), 'f': (0, 20)}, 'group': 'Inflamacion',
     'plain': 'Otro marcador de inflamacion, mas lento de moverse que la PCR.',
     'peptides': ['reparacion', 'inmune']},
    {'key': 'leucocitos', 'label': 'Leucocitos', 'unit': 'x10^3/uL', 'low': 4.5, 'high': 11.0, 'group': 'Biometria',
     'plain': 'Los globulos blancos: las celulas de defensa.',
     'peptides': ['inmune', 'reparacion']},
    {'key': 'linfocitos', 'label': 'Linfocitos', 'unit': '%', 'low': 20, 'high': 40, 'group': 'Biometria',
     'plain': 'La fraccion de defensas encargada de la memoria inmunologica.',
     'peptides': ['inmune']},
    {'key': 'neutrofilos', 'label': 'Neutrofilos', 'unit': '%', 'low': 40, 'high': 70, 'group': 'Biometria',
     'plain': 'La primera linea de defensa contra bacterias.',
     'peptides': ['inmune']},
    {'key': 'hemoglobina', 'label': 'Hemoglobina', 'unit': 'g/dL', 'sexed': {'m': (13.5, 17.5), 'f': (12.0, 15.5)}, 'group': 'Biometria',
     'plain': 'La proteina que transporta oxigeno en los globulos rojos.',
     'peptides': ['reparacion', 'inmune', 'hormonal']},
    {'key': 'hematocrito', 'label': 'Hematocrito', 'unit': '%', 'sexed': {'m': (38.8, 50.0), 'f': (34.9, 44.5)}, 'group': 'Biometria',
     'plain': 'Que porcentaje de la sangre son globulos rojos.',
     'peptides': ['hormonal', 'reparacion']},
    {'key': 'plaquetas', 'label': 'Plaquetas', 'unit': 'x10^3/uL', 'low': 150, 'high': 450, 'group': 'Biometria',
     'plain': 'Las celulas que forman coagulos.',
     'peptides': ['reparacion']},

    # --- Micronutrientes y cobre ---
    {'key': 'cobre_serico', 'label': 'Cobre serico', 'unit': 'ug/dL', 'low': 70, 'high': 140, 'group': 'Micronutrientes',
     'plain': 'El cobre disponible en sangre. Relevante si se investiga con complejos de cobre.',
     'peptides': ['reparacion']},
    {'key': 'ceruloplasmina', 'label': 'Ceruloplasmina', 'unit': 'mg/dL', 'low': 20, 'high': 35, 'group': 'Micronutrientes',
     'plain': 'La proteina que transporta el cobre.',
     'peptides': ['reparacion']},
    {'key': 'vitamina_d', 'label': 'Vitamina D (25-OH)', 'unit': 'ng/mL', 'low': 30, 'high': 100, 'group': 'Micronutrientes',
     'plain': 'Reserva de vitamina D. Muy baja en poblacion general.',
     'peptides': ['inmune', 'reparacion', 'antioxidante']},
    {'key': 'vitamina_b12', 'label': 'Vitamina B12', 'unit': 'pg/mL', 'low': 200, 'high': 900, 'group': 'Micronutrientes',
     'plain': 'Vitamina clave para nervios y formacion de sangre.',
     'peptides': ['antioxidante', 'cognitivo']},
    {'key': 'ferritina', 'label': 'Ferritina', 'unit': 'ng/mL', 'sexed': {'m': (24, 336), 'f': (11, 307)}, 'group': 'Micronutrientes',
     'plain': 'La reserva de hierro del cuerpo. Tambien sube con inflamacion.',
     'peptides': ['reparacion', 'inmune']},
]

MARKERS_BY_KEY = {m['key']: m for m in MARKERS}


def range_for(marker: dict, sex: str = None):
    """Rango (low, high) del marcador para ese sexo. Cae al general si no aplica."""
    sexed = marker.get('sexed')
    if sexed:
        return sexed.get('f' if sex == 'female' else 'm')
    return marker.get('low'), marker.get('high')


def evaluate(key: str, value, sex: str = None):
    """Clasifica un valor: 'bajo', 'normal', 'alto' o None si no lo conocemos."""
    marker = MARKERS_BY_KEY.get(key)
    if not marker or value is None:
        return None
    low, high = range_for(marker, sex)
    if low is None or high is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < low:
        return 'bajo'
    if v > high:
        return 'alto'
    return 'normal'


def families_for_products(names) -> set:
    """Familias de compuesto que le tocan a este cliente segun lo que compro o planea."""
    import re
    blob = ' '.join(names or '').lower() if isinstance(names, str) else ' '.join(n.lower() for n in (names or []))
    return {fam for fam, pattern in FAMILIES.items() if re.search(pattern, blob)}


def relevant_markers(families: set) -> list:
    """Marcadores pertinentes para esas familias. Sin familias, no hay nada que mostrar."""
    if not families:
        return []
    return [m for m in MARKERS if families.intersection(m['peptides'])]
