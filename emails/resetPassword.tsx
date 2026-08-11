import {
  Body,
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

// Botão "bulletproof": o Outlook clássico (motor Word) não respeita bem
// display:inline-block/padding num <a> — o resultado real é o botão a não aparecer de todo
// (não só sem estilo, ausente). O <Button> do react-email só tem o truque dos caracteres
// invisíveis para simular padding, que não chega. Aqui geram-se DOIS botões no mesmo HTML: um
// <v:roundrect> VML (só o Outlook o vê, via <!--[if mso]-->) e um <a> normal para todos os
// outros clientes (escondido do Outlook via <!--[if !mso]><!--> ... <!--<![endif]-->). Tudo
// num único bloco de HTML em bruto — dividir isto por vários elementos React quebraria a
// fronteira dos comentários condicionais (cada elemento fecha-se a si próprio).
//
// href fica com o placeholder https://example.com de propósito: users/notifications.py
// substitui-o pelo link real (uid+token) depois do HTML já gerado — ver esse ficheiro.
function bulletproofButtonHtml(href: string, label: string, widthPx: number): string {
  return `<!--[if mso]>
<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="${href}" style="height:50px;v-text-anchor:middle;width:${widthPx}px;" arcsize="50%" strokecolor="#0312c2" fillcolor="#0312c2">
<w:anchorlock/>
<center style="color:#ffffff;font-family:Arial,sans-serif;font-size:15px;font-weight:bold;">${label}</center>
</v:roundrect>
<![endif]-->
<!--[if !mso]><!-->
<a href="${href}" target="_blank" style="font-family:'Gellix', Arial, sans-serif;background-color:#0312c2;color:#ffffff;font-size:15px;font-weight:700;border-radius:100px;padding:16px 32px;text-decoration:none;display:inline-block;">${label}</a>
<!--<![endif]-->`;
}

export default function ResetPassword() {
  return (
    <Html>
      <Head />
      <Preview>Alterar password</Preview>
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
            <Heading style={heroHeading}>Alterar password</Heading>
            <Text style={heroText}>
              Alguém solicitou a redefinição de senha da sua conta. Use o
              botão abaixo para escolher uma nova password.
            </Text>
            <div
              dangerouslySetInnerHTML={{
                __html: bulletproofButtonHtml('https://example.com', 'Alterar password', 230),
              }}
            />

            <Text style={{ ...bodyParagraph, marginTop: '32px' }}>
              Se não solicitou a alteração de password, por favor, ignore este e-mail.
              A password não será alterada até que acesse o link acima e crie uma nova.
            </Text>
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

const body = {
  backgroundColor: '#ffffff',
  padding: '32px 40px 40px',
  borderTop: '4px solid #ebf5fc',
};

const bodyParagraph = {
  fontFamily: FONT,
  fontSize: '13px',
  lineHeight: '1.6',
  color: '#666666',
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
