# Flowise — fork Xmacna

A linha canônica é `main`, consolidada a partir de `8289ae0ae` (inclui o revert do cache fix).
Não atualizar upstream ou reintroduzir `187619a6c` como consequência de higiene Git.

-   Runtime base: Flowise 3.0.13, Node 20.19.5, pnpm 10.26.0. Instalar com lock congelado.
-   Verificação: `pnpm test:coverage`, `pnpm build`, `python3 -m unittest discover -s scripts/xmacna -p 'test_*.py' -v`.
-   Produção não segue automaticamente a main. Push/merge nunca autoriza restart, `latest` ou deploy.
-   Releases: [docs/xmacna/releases.md](docs/xmacna/releases.md). Workflow manual publica candidato
    por SHA/digest; promoção usa uma application version existente, canário e rollback imutáveis.
-   AWS: conta 387653681120, região us-east-1; localmente usar `AWS_PROFILE=xmacna`.
-   Skills, segredos e consumidores vivem no pai Elysium; operações via CLI `xmacna`, skill `flowise`.
-   Backup privado antes de tocar configuração viva. Nunca commitar `.env`, bundles EB vivos ou chaves.
-   O bundle vivo de setembro/2026 ainda usa `latest`; sua equivalência com esta fonte não foi provada.
    Health/versão da API não substituem identidade da imagem ou comparação de fonte.
