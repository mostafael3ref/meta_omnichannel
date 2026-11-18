import { createApp } from "vue";
import App from "./App.vue";
import "./styles.css";

const el = document.getElementById("omnichannel-app");
if (el) {
  createApp(App).mount(el);
}
