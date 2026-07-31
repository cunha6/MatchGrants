import {
  Body,
  Button,
  Column,
  Container,
  Head,
  Heading,
  Html,
  Img,
  Link,
  Preview,
  Row,
  Section,
  Text,
} from '@react-email/components';
import * as React from 'react';

const FONT = "'Gellix', Arial, sans-serif";

interface Anuncio {
  description: string;
  precoBase: string;
  prazo: string;
  url: string;
}

interface AnunciosPublicadosProps {
  anuncios: Anuncio[];
}

// Dados de exemplo: servem de pré-visualização (`npm run dev`) e de valor por
// omissão na exportação para HTML (`npm run export`), que renderiza sem props.
const anunciosExemplo: Anuncio[] = [
  {
    description: 'Consultoria para elaboração de um plano estratégico de sustentabilidade ambiental',
    precoBase: '85 mil €',
    prazo: '15/09/2026',
    url: 'http://localhost:5173/anuncios/101',
  },
  {
    description: 'Estudo de viabilidade económica e financeira para investimento em internacionalização',
    precoBase: '120 mil €',
    prazo: '30/09/2026',
    url: 'http://localhost:5173/anuncios/102',
  },
  {
    description: 'Assessoria técnica para candidatura a fundos europeus na área da economia circular',
    precoBase: '45 mil €',
    prazo: '10/10/2026',
    url: 'http://localhost:5173/anuncios/103',
  },
];

export default function AnunciosPublicados({
  anuncios = anunciosExemplo,
}: AnunciosPublicadosProps) {
  const plural = anuncios.length > 1;

  return (
    <Html>
      <Head />
      <Preview>
        {plural ? 'Novos anúncios publicados' : 'Novo anúncio publicado'} — candidaturas a decorrer
      </Preview>
      <Body style={main}>
        <Container style={container}>

          {/* HERO */}
          <Section style={hero}>
            <Img
              src="https://www.co2-prestatieladder.nl/app/uploads/sites/2/2024/12/PT-Aliados-logo.png"
              height="57"
              alt="Aliados"
              style={{ display: 'block' }}
            />
            <Heading style={heroHeading}>
              {plural ? 'Novos anúncios publicados' : 'Novo anúncio publicado'}
            </Heading>
            <Text style={heroText}>
              {plural
                ? 'Foram publicados novos anúncios de procedimentos que podem enquadrar-se nos seus projetos. Consulte abaixo o preço base e o prazo de propostas de cada um.'
                : 'Foi publicado um novo anúncio de procedimento que pode enquadrar-se nos seus projetos. Consulte abaixo o preço base e o prazo de propostas.'}
            </Text>
            <Button style={pillButton} href="http://localhost:5173/newsletter/">
              {plural ? 'Ver detalhes dos anúncios' : 'Ver detalhes do anúncio'}
            </Button>
          </Section>

          {/* TABELA DE ANÚNCIOS */}
          <Section style={body}>
            <Row>
              <Column style={{ ...headerCell, width: '60%' }}>
                <Text style={headerText}>Descrição</Text>
              </Column>
              <Column style={{ ...headerCell, width: '25%' }}>
                <Text style={headerText}>Preço base</Text>
              </Column>
              <Column style={{ ...headerCell, width: '15%' }}>
                <Text style={headerText}>Prazo</Text>
              </Column>
            </Row>

            {anuncios.map((anuncio) => (
              <Row key={anuncio.url}>
                <Column style={{ ...cell, width: '60%' }}>
                  <Link href={anuncio.url} style={tituloLink}>
                    {anuncio.description}
                  </Link>
                </Column>
                <Column style={{ ...cell, width: '25%' }}>
                  <Text style={cellText}>{anuncio.precoBase}</Text>
                </Column>
                <Column style={{ ...cell, width: '15%' }}>
                  <Text style={cellText}>{anuncio.prazo}</Text>
                </Column>
              </Row>
            ))}
          </Section>

          {/* FOOTER */}
          <Section style={footer}>
            <Text style={slogan}>"Impacting the future <br /> Together."</Text>
            <Row>
              <Column style={footerColLeft}>
                <Text style={address}>
                  Fábrica Santo Thyrso · Av. da Fábrica de Santo Tirso Nº 88, Sala B1 <br />
                  4780-257 Santo Tirso, Portugal
                </Text>
                <Text style={footerLinks}>
                  <Link href="mailto:hello@aliados.consulting" style={footerLink}>hello@aliados.consulting</Link>
                </Text>
                <Text style={{ ...footerLinks, marginTop: '4px' }}>
                  <Link href="https://www.linkedin.com/company/aliadosconsulting" style={footerLink}>LinkedIn</Link>
                  {'   '}
                  <Link href="https://www.aliados.consulting" style={footerLink}>Website</Link>
                </Text>
              </Column>
              <Column style={footerColRight}>
                <Text style={legal}>
                  © {new Date().getFullYear()} Aliados Consulting. Todos os direitos reservados.
                </Text>
                <Text style={unsubscribe}>
                  <Link href="#" style={unsubscribeLink}>Unsubscribe</Link>{' '}
                  from marketing emails.
                </Text>
              </Column>
            </Row>
          </Section>

        </Container>
      </Body>
    </Html>
  );
}

AnunciosPublicados.PreviewProps = { anuncios: anunciosExemplo } as AnunciosPublicadosProps;

// --- Estilos (paleta/tipografia Aliados: Gellix, #0312c2 / #5cd9ba / #ebf5fc) ---
// fontFamily é declarado em CADA elemento de texto (não só no Body) — a herança de CSS
// não é fiável em clientes de email (Outlook em particular reinicia a fonte a cada tabela).
const main = {
  backgroundColor: '#f4f4f4',
  fontFamily: FONT,
};

const container = {
  margin: '0 auto',
  maxWidth: '600px',
  backgroundColor: '#ffffff',
};

const hero = {
  backgroundColor: '#5cd9ba',
  padding: '40px 40px 48px',
};

const heroHeading = {
  fontFamily: FONT,
  fontSize: '36px',
  lineHeight: '1.15',
  letterSpacing: '-1px',
  fontWeight: '700',
  color: '#0312c2',
  margin: '28px 0 16px',
};

const heroText = {
  fontFamily: FONT,
  fontSize: '16px',
  lineHeight: '1.6',
  color: 'rgba(3,18,194,0.85)',
  margin: '0 0 28px',
};

const pillButton = {
  fontFamily: FONT,
  backgroundColor: '#0312c2',
  color: '#ffffff',
  fontSize: '15px',
  fontWeight: '700',
  borderRadius: '100px',
  padding: '16px 32px',
  textDecoration: 'none',
  display: 'inline-block',
};

const body = {
  backgroundColor: '#ffffff',
  padding: '40px',
  borderTop: '4px solid #ebf5fc',
};

const headerCell = {
  borderBottom: '2px solid #0312c2',
  paddingBottom: '8px',
  verticalAlign: 'bottom' as const,
};

const headerText = {
  fontFamily: FONT,
  fontSize: '11px',
  fontWeight: '700',
  letterSpacing: '0.5px',
  textTransform: 'uppercase' as const,
  color: '#0312c2',
  margin: '0',
};

const cell = {
  borderBottom: '1px solid #ebf5fc',
  paddingTop: '14px',
  paddingBottom: '14px',
  verticalAlign: 'top' as const,
};

const tituloLink = {
  fontFamily: FONT,
  fontSize: '14px',
  lineHeight: '1.4',
  fontWeight: '600',
  color: '#0312c2',
  textDecoration: 'underline',
};

const cellText = {
  fontFamily: FONT,
  fontSize: '14px',
  lineHeight: '1.4',
  color: '#333333',
  margin: '0',
};

const footer = {
  backgroundColor: '#ebf5fc',
  padding: '40px',
  textAlign: 'center' as const,
};

const slogan = {
  fontFamily: FONT,
  fontSize: '22px',
  lineHeight: '1.4',
  fontStyle: 'italic',
  fontWeight: '700',
  color: '#0312c2',
  margin: '0 0 20px',
};

const footerColLeft = {
  width: '50%',
  verticalAlign: 'top' as const,
  textAlign: 'left' as const,
  paddingRight: '12px',
};

const footerColRight = {
  width: '50%',
  verticalAlign: 'top' as const,
  textAlign: 'right' as const,
  paddingLeft: '12px',
};

const address = {
  fontFamily: FONT,
  fontSize: '12px',
  lineHeight: '1.6',
  color: '#333333',
  margin: '0 0 12px',
};

const footerLinks = {
  fontFamily: FONT,
  fontSize: '12px',
  lineHeight: '1.6',
  color: '#333333',
  margin: '0',
};

const footerLink = {
  fontFamily: FONT,
  color: '#0312c2',
  textDecoration: 'none',
};

const legal = {
  fontFamily: FONT,
  fontSize: '11px',
  lineHeight: '1.6',
  color: '#666666',
  margin: '0 0 8px',
};

const unsubscribe = {
  fontFamily: FONT,
  fontSize: '11px',
  color: '#666666',
  margin: '0',
};

const unsubscribeLink = {
  fontFamily: FONT,
  color: '#0312c2',
  textDecoration: 'underline',
};
