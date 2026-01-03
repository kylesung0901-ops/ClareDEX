import { initializeApp } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js";
import { getAnalytics } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-analytics.js";

const firebaseConfig = {
    apiKey: "AIzaSyCinbNPl7nkNN9azWsdYackkxjAcp7uF-o",
    authDomain: "clare-dex.firebaseapp.com",
    projectId: "clare-dex",
    storageBucket: "clare-dex.firebasestorage.app",
    messagingSenderId: "874861072951",
    appId: "1:874861072951:web:e46a62c82c0447a170a5b2",
    measurementId: "G-3TJ4NB41RX"
};

// Initialize Firebase
const app = initializeApp(firebaseConfig);
const analytics = getAnalytics(app);

console.log("Firebase initialized successfully");
