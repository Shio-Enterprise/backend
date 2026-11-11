# #91 — validação e padronização de CPF

O perfil aceita CPF válido com ou sem pontuação, grava os 11 dígitos e devolve
`000.000.000-00` pela API. O campo vazio continua sendo `NULL`. A validação confere
os dois dígitos verificadores e recusa sequências repetidas. Cadastro e login por
Google não recebem CPF; ambos continuam usando a edição de perfil para informá-lo.

## Dados antigos

A migração `authentication.0006_normalize_profile_cpf` agrupa os CPFs antes de
padronizar. Valores válidos e sem conflito ficam só com dígitos; vazios viram
`NULL`. CPFs inválidos e todos os perfis de um grupo duplicado permanecem com os
valores originais, sem exclusão de contas ou escolha automática de um titular.
O campo mantém `max_length=14` para preservar esses casos.

Durante `migrate`, as linhas com prefixo `[CPF #91]` listam e-mails e IDs dos
perfis que precisam de revisão. O relatório não imprime o CPF. Guarde essa saída
para a equipe resolver os casos; não há aviso novo ao cliente nem correção
automática de titularidade. A validação de duplicidade considera também a
pontuação dos registros preservados.

Antes de aplicar em produção, rode a migração em um banco isolado com cópia dos
dados reais e revise o relatório. Os testes automatizados usam apenas dados
sintéticos. A reversão não restaura a pontuação antiga dos CPFs normalizados.

## Integrações e testes

Os Correios recebem só números, inclusive para os registros legados preservados.
O payload atual da InfinitePay não inclui CPF e permanece com nome, e-mail e
telefone. “Meus Dados” usa o CPF formatado devolvido pela API após salvar e mantém
os erros associados ao campo por `aria-describedby` e `aria-invalid`.

Os testes cobrem os dígitos verificadores, repetição, tamanho, valor vazio,
duplicidade entre formatos, atualização e limpeza do perfil, a migração real com
duplicados, o payload dos Correios e a exibição/erro do campo no frontend.
