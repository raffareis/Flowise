# Releases do fork Flowise

A main consolida a linha operacional Git `8289ae0ae`, preservando o revert de `187619a6c`.
Isso não prova que a imagem viva foi construída desse commit. Nenhum deploy acompanha a consolidação.

## Candidato

`.github/workflows/xmacna-prod-aws.yml` é exclusivamente manual e só opera a ref `main`.
`operation=candidate` exige Node CI e Docker Build verdes no mesmo SHA, publica uma tag única
`candidate-SHA-run-attempt` e registra o digest. Não atualiza `latest`, EB ou configurações.
Node/pnpm são fixados; o lock inclui a integridade do tarball oficial SheetJS 0.20.3.

## Promoção e rollback

A aplicação EB é `flowise`, ambiente `flowise-prod`, ECR `flowise` na conta 387653681120/us-east-1.
Preparar uma application version **existente e revisada**, cujo bundle use
`387653681120.dkr.ecr.us-east-1.amazonaws.com/flowise@sha256:...` em Dockerrun v1.
Preservar e comparar o bundle/configuração vivo, inclusive ebextensions, volumes e hooks;
não substituí-lo por um template local. Os bundles podem conter segredos: armazenamento privado,
sem upload ao Git ou artefatos públicos. A preparação inicial do baseline imutável é uma operação
separada, dependente de prova da imagem viva; este fluxo não faz essa conversão automaticamente.

`operation=plan` no workflow é read-only. A promoção exige versão/digest alvo, versão/digest
atuais exatos, hashes SHA256 revisados dos dois bundles e um ambiente canário distinto, da mesma aplicação, Ready/Green já na versão alvo.
O script baixa os dois bundles, valida hashes e digests no ECR e recusa qualquer mudança de
configuração/conteúdo/permissões além do digest da imagem; imediatamente antes da escrita relê os
bundles e o estado para detectar drift. `promote` escreve apenas `update-environment --version-label`,
aguarda Ready/Green e HTTP 200. Não altera env vars, não executa restart e não cria recursos.

O canário deve usar dados isolados e ter suas migrations/predictions/integradores verificados antes
da promoção. Ready/Green só é um gate técnico adicional; não é prova completa de comportamento.
O registro é persistido e relido no bucket privado EB, com SSE-S3 e chave única, antes da escrita
em produção; contém a versão/digest/bundle anterior. Falha dessa persistência bloqueia a promoção. Falha não provoca
rollback automático: analisar migrations antes de retornar a uma versão anterior. Para rollback
explícito, usar `operation=rollback`, com alvo/anterior trocados e estado corrente conferido;
permite health degradado, mas exige ambiente Ready e os dois bundles imutáveis. Mantém todas as
outras validações, dispensa canário e produz novo registro. A concorrência serializa workflows
GitHub; operadores externos ainda precisam respeitar a mesma janela (a API EB não oferece CAS).

Localmente, o mesmo script é read-only por padrão; `--live` exige `--evidence` com caminho novo:

```bash
AWS_PROFILE=xmacna python3 scripts/xmacna/release.py \
  --version VERSAO_ALVO --digest sha256:DIGEST_ALVO \
  --expected-current VERSAO_ATUAL --current-digest sha256:DIGEST_ATUAL \
  --bundle-sha256 HASH_BUNDLE_ALVO --current-bundle-sha256 HASH_BUNDLE_ATUAL \
  --canary AMBIENTE_CANARIO
```

## Estado observado em 19/09/2026

Produção: Ready/Green, `v2-20260304-211946`, API 3.0.13 e DB OK. O bundle vivo tem SHA256
`c66f90c36b94a201068139b54ce0686fc5ea8df62f859664fb0d0b6e3d014bd5` e aponta a `flowise:latest`.
Portanto, **a promoção permanece bloqueada** até haver baseline/rollback imutável e prova canário.
Não interpretar a main consolidada como uma nova imagem em produção. Backups e evidência privada
ficam no pai Elysium; o handoff registra testes, SHAs e o estado da limpeza.

Contrato AWS: [application versions](https://docs.aws.amazon.com/cli/latest/reference/elasticbeanstalk/update-environment.html)
e [Dockerrun](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/single-container-docker-configuration.html).
