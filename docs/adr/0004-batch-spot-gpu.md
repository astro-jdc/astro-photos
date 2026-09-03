# 0004 — AWS Batch sobre spot GPU frente a SageMaker

Estado: Aceptado · 2026-09-03

## Contexto

Las reconstrucciones son trabajos por lotes: minutos u horas de cómputo, sin
interactividad, con demanda muy irregular (cero durante días, y una avalancha cuando
alguien comparte un enlace). El entrenamiento es aún más esporádico.

El coste tiene que ser **cero cuando nadie usa el sistema**. Y el mismo código tiene
que poder correr en el portátil del desarrollador y en la Intel Arc B70 local.

## Decisión

**AWS Batch** con un compute environment de spot GPU (g5/g6) y `minvCpus=0`, disparado
por una Lambda que consume de la cola SQS `reconstruct`. El contenedor es la misma
imagen de `models/` que se usa en local.

El desarrollo y la iteración de entrenamiento se hacen **en local sobre la Arc B70**
(torch XPU); las tiradas largas van a Batch.

## Consecuencias

- Sin trabajo en cola no hay ninguna instancia encendida. El coste en reposo es 0.
- Spot ahorra en torno al 70 %, a cambio de interrupciones: los jobs son idempotentes
  y reintentables, y llevan checkpoint.
- Portabilidad real: un `docker run` reproduce en local exactamente lo que corre en AWS.
- Hay que gestionar el arranque en frío (2–5 minutos hasta que la instancia está lista)
  y comunicarlo en la UI como tiempo estimado.

## Alternativas descartadas

- **SageMaker.** Más caro por hora, más lock-in, y su ventaja (endpoints gestionados,
  experiment tracking) no compensa cuando la carga es por lotes y esporádica.
- **Lambda con GPU.** No existe; y el límite de 15 minutos excluiría las
  reconstrucciones grandes.
- **Fargate con GPU.** No disponible. Fargate se queda para la API.
