## Objetivo do PR
*Descreva brevemente o que este PR resolve ou adicione "Closes #ID_DA_ISSUE" para vincular e fechar a issue automaticamente.*

## Definition of Done (DoD) - Checklist

**Antes de solicitar revisão, confirme se as etapas abaixo foram cumpridas marcando com um "x":**

### Código e Qualidade
- [ ] O código implementado resolve a falha ou entrega a melhoria sem introduzir novos bugs (regressão) no sistema legado.
- [ ] A solução cumpre **todos** os Critérios de Aceite definidos na issue associada.
- [ ] A pipeline de automação (CI/CD / GitHub Actions) executou e passou com sucesso.
- [ ] O código está atualizado e livre de conflitos com a branch principal (`main`).

### Documentação e Rastreabilidade
- [ ] A tabela da Sprint no repositório `Documentacao` foi atualizada com a atividade resolvida, os responsáveis e a origem da demanda.
- [ ] A tarefa correspondente no quadro do **GitHub Projects** está atualizada e os responsáveis (*Assignees*) estão marcados corretamente.

### Entrega e Deploy (Ações pós-merge)
- [ ] Estou ciente de que, após o merge, devo gerar uma nova **Release** (ex: `v1.0.0-sprint1`) no repositório correspondente.
- [ ] O deploy no ambiente de **Homologação/Staging** foi/será atualizado para garantir que o professor consiga avaliar a correção rodando na nuvem.

---
### Para o Revisor (Colega do Trio)
- [ ] Revisei o código, confirmo que atende às regras de negócio e aprovo as alterações.
