# Página inicial editável — desenho

## Intenção

Permitir que administradores autorizados atualizem o carrossel e a frase da faixa preta da loja para cada campanha, sem publicação do frontend.

## Escopo

- Nova tela **Página inicial** no painel, com lista ordenável de banners, formulário de imagem para computador, imagem opcional para celular, texto alternativo e link opcional, além do campo da frase.
- A loja lê banners e frase de uma API pública a cada carregamento. Se a lista persistida estiver vazia, usa as duas imagens locais atuais. A frase padrão continua sendo “Deus primeiro. Negócios depois.” quando ainda não houver configuração.
- A faixa conserva fundo preto e texto branco. Agendamento e edição das outras seções ficam fora desta entrega.

## Abordagens

1. **Operações individuais por banner (escolhida):** criação e edição com multipart, exclusão individual e endpoint para salvar a ordem; a frase tem atualização própria. Aproveita o padrão de upload atual e limita o impacto de cada edição. A tela deve indicar claramente quando uma ação já foi publicada.
2. **Salvamento integral da homepage:** envia frase, ordem, novos arquivos e exclusões em uma operação. Um botão único simplifica o conceito de publicação, mas exige protocolo mais complexo para misturar arquivos e registros existentes e para recuperar falhas.

O solicitante escolheu a abordagem 1.

## Modelo e API

- No backend Django, uma configuração única guarda a frase; cada banner guarda `id`, `image_desktop`, `image_mobile` opcional, `alt_text`, `link` opcional e `display_order`.
- `GET /api/catalog/homepage/` é público, sem paginação, e devolve frase e banners ordenados. Respostas de leitura não devem ficar em cache além do recarregamento esperado.
- Endpoints administrativos sob `/api/catalog/homepage/` permitem criar, editar, remover e reordenar banners e atualizar a frase. Todos exigem autenticação e a nova permissão `manage_homepage`; pedidos sem login recebem 401 e contas sem permissão, 403.
- Imagens aceitas: JPEG, PNG e WebP, até 5 MB por arquivo, com validação do conteúdo real. Erros de upload indicam o campo e a causa. Ao substituir ou remover imagens, o armazenamento antigo é limpo somente após sucesso da alteração.
- A ordenação é enviada como lista completa de IDs existentes, sem repetição, para ser validada e aplicada em transação. IDs desconhecidos, repetidos ou ausentes produzem 400 sem alteração parcial.
- A permissão entra na lista de permissões configuráveis, no roteamento protegido e na navegação do admin. Administradores existentes com acesso integral preservam acesso; contas com permissões limitadas precisam receber a nova permissão explicitamente.

## Decisões

- O solicitante restringiu links a caminhos internos da loja. A validação aceita caminhos iniciados por `/`, sem `//`, para evitar redirecionamentos externos ou esquemas perigosos.
- A frase pode ser alterada independentemente dos banners; texto vazio é recusado. A configuração ausente usa a frase atual.
- A imagem de celular é opcional; na ausência dela, a loja reutiliza a imagem de computador em todas as larguras.
- Se a API pública estiver temporariamente indisponível, a loja mostra os banners e a frase padrão, mantendo a página utilizável.

## Verificação

- Testes da API para 401/403, permissões, CRUD, ordem inválida, limite e formato de imagem, fallback sem cadastro e leitura após atualização.
- Testes React para formulário, controles de ordem, link do banner, imagem responsiva, frase configurada e fallback.
- Build do frontend e testes relevantes dos dois repositórios.
